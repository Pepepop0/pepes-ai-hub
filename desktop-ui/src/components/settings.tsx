import { useEffect, useState, type FormEvent } from 'react';
import { Activity as ActivityIcon, CheckCircle2, CircleAlert, Copy, ExternalLink, FileText, Globe2, LogOut, Monitor, Moon, RefreshCw, Search, Shield, Stethoscope, Sun } from 'lucide-react';
import type { OperationEvent, WebSearchStatus } from '../lib/api';
import { useCopy } from '../lib/clipboard';
import { useDesktop } from '../lib/desktop';
import { cn, date, label } from '../lib/utils';
import { Badge, Button, Empty } from './ui';
import { OperationCard } from './operation';
import { CredentialCard } from './credential-card';

export type Theme = 'dark' | 'light' | 'system';
const THEME_KEY = 'pepitos.desktop.theme.v1';
const themes = [{value: 'dark', label: 'Escuro', Icon: Moon}, {value: 'light', label: 'Claro', Icon: Sun}, {value: 'system', label: 'Sistema', Icon: Monitor}] as const;
const SEARCH_KEY_URLS = {
  brave: 'https://api.search.brave.com/app/keys',
  unsplash: 'https://unsplash.com/oauth/applications',
  pexels: 'https://www.pexels.com/api/new/',
} as const;
type SearchKeyProvider = keyof typeof SEARCH_KEY_URLS;
const SEARCH_PROVIDERS: {id: SearchKeyProvider; name: string; description: string; placeholder: string}[] = [
  {id: 'brave', name: 'Brave Search', description: 'Texto e páginas; usado quando não há SearXNG.', placeholder: 'Chave da API do Brave'},
  {id: 'unsplash', name: 'Unsplash', description: 'Fotos para a busca de referências; exige sua própria chave.', placeholder: 'Access Key do Unsplash'},
  {id: 'pexels', name: 'Pexels', description: 'Fotos para a busca de referências; exige sua própria chave.', placeholder: 'Chave da API do Pexels'},
];
const ENGINE_LABEL = {duckduckgo: 'DuckDuckGo', searxng: 'SearXNG', brave: 'Brave Search'} as const;

export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem(THEME_KEY);
    return saved === 'light' || saved === 'system' ? saved : 'dark';
  });
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    function apply() {
      const effective = theme === 'system' ? (media.matches ? 'dark' : 'light') : theme;
      document.documentElement.dataset.theme = effective;
      document.querySelector('meta[name="theme-color"]')?.setAttribute('content', effective === 'dark' ? '#17181b' : '#f4f3ef');
    }
    localStorage.setItem(THEME_KEY, theme);
    apply();
    media.addEventListener('change', apply);
    return () => media.removeEventListener('change', apply);
  }, [theme]);
  return {theme, setTheme};
}

function matchesFilter(event: OperationEvent, filter: string) {
  if (filter === 'all') return true;
  if (filter === 'errors') return event.state === 'failed' || Boolean(event.diagnostic);
  return event.kind === filter;
}

export function Activity() {
  const {state, run, pending} = useDesktop();
  const copy = useCopy();
  const [filter, setFilter] = useState('all');
  const [doctor, setDoctor] = useState<string | null>(null);
  const events = (state?.events ?? []).filter(event => matchesFilter(event, filter)).reverse();
  async function diagnose() {
    const result = await run<{text: string}>('doctor', {}, {failure: 'O diagnóstico não pôde ser executado'});
    if (result) setDoctor(result.value.text);
  }
  return <>
    <div className="section-heading">
      <p className="muted">O diagnóstico lê instalação, serviços, GPU e sistema sem alterar nada.</p>
      <Button variant="secondary" onClick={() => void diagnose()} disabled={Boolean(pending)} busy={pending === 'doctor'} busyLabel="Executando diagnóstico…"><Stethoscope size={16} aria-hidden="true" />Executar diagnóstico</Button>
    </div>
    <OperationCard operation={state?.operation ?? null} />
    {doctor !== null ? <section className="card" aria-label="Diagnóstico do ambiente">
      <div className="section-heading"><h3>Diagnóstico do ambiente</h3><div className="actions"><Button variant="secondary" size="sm" disabled={!doctor} onClick={() => void copy(doctor ?? '', 'Relatório de diagnóstico')}><Copy size={15} aria-hidden="true" />Copiar relatório</Button><Button variant="ghost" size="sm" onClick={() => setDoctor(null)}>Ocultar relatório</Button></div></div>
      <pre className="doctor-output">{doctor || 'O diagnóstico não retornou texto.'}</pre>
    </section> : null}
    <section className="card">
      <div className="section-heading">
        <div><h3>Linha do tempo</h3><p className="muted small">Histórico limitado aos eventos retidos pelo serviço local.</p></div>
        <select aria-label="Filtrar eventos" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">Todos os eventos</option><option value="errors">Falhas e diagnósticos</option><option value="activity">Atividades</option><option value="progress">Progresso</option><option value="result">Resultados</option></select>
      </div>
      {events.length ? <ol className="timeline">{events.map(event => {
        const activity = event.activity || event.detail;
        const failed = event.state === 'failed';
        return <li key={`${event.operation_id}-${event.sequence}`}>
          <div className={`timeline-marker ${failed ? 'danger' : event.state === 'succeeded' ? 'success' : ''}`} aria-hidden="true">{event.state === 'succeeded' ? <CheckCircle2 size={15} /> : failed ? <CircleAlert size={15} /> : <ActivityIcon size={15} />}</div>
          <div className="grow">
            <div className="split wrap"><strong>{event.message}</strong><time dateTime={event.timestamp} className="small muted">{date(event.timestamp)}</time></div>
            <p className="small muted">{label(event.phase)} · {label(event.state)} · #{event.sequence}</p>
            {activity ? <p className="small">{activity.item}{activity.detail ? ` · ${activity.detail}` : ''}</p> : null}
            {event.diagnostic ? <details className="diagnostic"><summary>Ver diagnóstico</summary><pre>{JSON.stringify(event.diagnostic, null, 2)}</pre></details> : null}
          </div>
        </li>;
      })}</ol> : <Empty icon={<ActivityIcon size={30} />} title={filter === 'all' ? 'Nenhum evento nesta sessão' : 'Nenhum evento neste filtro'} description={filter === 'all' ? 'As atividades aparecem quando você instala, inicia, repara ou usa o ambiente.' : 'Selecione outro filtro para acompanhar o histórico da sessão.'} />}
    </section>
  </>;
}


interface WebSearchCardProps {
  status: WebSearchStatus | null;
  checked: boolean;
  address: string;
  keys: Record<SearchKeyProvider, string>;
  busy: boolean;
  locked: boolean;
  pending: string | null;
  working: (command: string | string[], args?: Record<string, unknown>) => boolean;
  setAddress: (value: string) => void;
  setKey: (provider: SearchKeyProvider, value: string) => void;
  refresh: () => void;
  configure: (enabled: boolean) => void;
  saveAddress: (event: FormEvent<HTMLFormElement>) => void;
  login: (provider: SearchKeyProvider, event: FormEvent<HTMLFormElement>) => void;
  logout: (provider: SearchKeyProvider) => void;
  openUrl: (url: string) => void;
}

function searchKeyConfigured(status: WebSearchStatus | null, provider: SearchKeyProvider) {
  if (provider === 'brave') return status?.brave_configured;
  if (provider === 'unsplash') return status?.unsplash_configured;
  return status?.pexels_configured;
}

function searchKeySource(status: WebSearchStatus | null, provider: SearchKeyProvider) {
  if (provider === 'brave') return status?.brave_source;
  if (provider === 'unsplash') return status?.unsplash_source;
  return status?.pexels_source;
}

function WebSearchCard(props: WebSearchCardProps) {
  const enabled = props.status?.enabled ?? true;
  return <section className="card search-settings-card">
    <div className="section-heading">
      <div className="inline"><div className="service-icon" aria-hidden="true"><Globe2 size={21} /></div><div><h3>Pesquisa web e referências</h3><p className="small muted">Disponível nos modelos Capella.</p></div></div>
      <div className="actions"><Badge tone={props.status ? (enabled ? 'success' : 'neutral') : 'neutral'}>{props.status ? (enabled ? ENGINE_LABEL[props.status.engine] : 'Desligada') : props.pending === 'web_search_status' ? 'Consultando…' : props.checked ? 'Indisponível' : 'Não consultado'}</Badge><Button variant="secondary" size="icon" aria-label="Atualizar configurações da pesquisa web" disabled={props.locked} busy={props.pending === 'web_search_status'} onClick={props.refresh}><RefreshCw size={16} aria-hidden="true" /></Button></div>
    </div>
    <div className="search-toggle-row">
      <div><strong>Permitir pesquisa</strong><p className="small muted">Desliga tanto a pesquisa por texto quanto a busca de fotos de referência.</p></div>
      <button type="button" role="switch" aria-label="Permitir pesquisa web" aria-checked={enabled} className={cn('switch', enabled && 'switch-on')} disabled={props.busy || !props.status} aria-busy={props.working('web_search_configure') || undefined} onClick={() => props.configure(!enabled)}>
        <span className="switch-thumb" aria-hidden="true" /><span className="switch-label"><Search size={14} aria-hidden="true" />{enabled ? 'Ligada' : 'Desligada'}</span>
      </button>
    </div>
    <form className="search-address" onSubmit={props.saveAddress}>
      <div className="field">
        <label htmlFor="searxng-address">Endereço do SearXNG <span className="muted">(opcional)</span></label>
        <div className="input-action"><input id="searxng-address" type="url" value={props.address} onChange={event => props.setAddress(event.target.value)} placeholder="http://localhost:8888" autoComplete="url" spellCheck={false} disabled={props.busy} /><Button type="submit" variant="secondary" disabled={props.busy}>Salvar endereço</Button></div>
        <p className="field-hint">Sem endereço ou chave, o Hub usa DuckDuckGo. Um SearXNG próprio é mais confiável para uso repetido.</p>
      </div>
    </form>
    <div className="search-provider-list">
      <div><strong>Chaves opcionais</strong><p className="small muted">Validada antes de salvar; protegida pelo Windows DPAPI. A chave nunca volta para a interface.</p></div>
      {SEARCH_PROVIDERS.map(provider => {
        const configured = Boolean(searchKeyConfigured(props.status, provider.id));
        const source = searchKeySource(props.status, provider.id);
        const fromEnvironment = Boolean(source && source !== 'windows-dpapi');
        const command = provider.id === 'brave' ? 'brave_login' : 'image_search_login';
        return <form key={provider.id} className="search-provider" onSubmit={event => props.login(provider.id, event)}>
          <div className="search-provider-heading"><div><strong translate="no">{provider.name}</strong><p className="small muted">{provider.description}</p>{fromEnvironment ? <p className="small muted">Origem: <code>{source}</code>. A variável de ambiente tem precedência.</p> : null}</div><Badge tone={configured ? 'success' : 'neutral'}>{fromEnvironment ? 'Ambiente' : configured ? 'Configurada' : 'Opcional'}</Badge></div>
          <div className="input-action"><input aria-label={`Chave do ${provider.name}`} type="password" value={props.keys[provider.id]} onChange={event => props.setKey(provider.id, event.target.value)} placeholder={provider.placeholder} autoComplete="off" spellCheck={false} disabled={props.busy} /><Button type="submit" size="sm" disabled={props.busy || !props.keys[provider.id].trim()} busy={props.working(command, provider.id === 'brave' ? {} : {provider: provider.id})} busyLabel="Validando…">Validar e salvar</Button></div>
          <div className="actions">
            <Button type="button" variant="ghost" size="sm" disabled={props.locked} onClick={() => props.openUrl(SEARCH_KEY_URLS[provider.id])}>Obter chave<ExternalLink size={13} aria-hidden="true" /></Button>
            {configured && !fromEnvironment ? <Button type="button" variant="ghost" size="sm" disabled={props.busy} onClick={() => props.logout(provider.id)}><LogOut size={14} aria-hidden="true" />Remover</Button> : null}
          </div>
        </form>;
      })}
    </div>
    <div className="notice"><RefreshCw size={17} aria-hidden="true" /><span>Depois de salvar ou remover uma chave do Unsplash/Pexels, reinicie os serviços para o Open WebUI receber a mudança.</span></div>
  </section>;
}

export function Settings({theme, setTheme}: {theme: Theme; setTheme: (value: Theme) => void}) {
  const {state, run, busy, pending, working} = useDesktop();
  const [webStatus, setWebStatus] = useState<WebSearchStatus | null>(null);
  const [webChecked, setWebChecked] = useState(false);
  const [searxngAddress, setSearxngAddress] = useState('');
  const [searchKeys, setSearchKeys] = useState<Record<SearchKeyProvider, string>>({brave: '', unsplash: '', pexels: ''});
  const locked = Boolean(pending);

  async function refreshWebSearch() {
    const result = await run<WebSearchStatus>('web_search_status', {}, {failure: 'Não foi possível consultar a pesquisa web'});
    if (result) {
      setWebStatus(result.value);
      setSearxngAddress(result.value.searxng_url);
    }
    setWebChecked(true);
  }
  useEffect(() => { void refreshWebSearch(); }, []);
  async function configureWebSearch(enabled: boolean, address = searxngAddress) {
    const result = await run<WebSearchStatus>('web_search_configure', {enabled, searxng_url: address}, {success: enabled ? 'Pesquisa web ligada' : 'Pesquisa web desligada', failure: 'Não foi possível salvar a pesquisa web'});
    if (result) {
      setWebStatus(result.value);
      setSearxngAddress(result.value.searxng_url);
    }
  }
  function saveSearchAddress(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void configureWebSearch(webStatus?.enabled ?? true);
  }
  function setSearchKey(provider: SearchKeyProvider, value: string) {
    setSearchKeys(current => ({...current, [provider]: value}));
  }
  async function loginSearch(provider: SearchKeyProvider, event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const token = searchKeys[provider].trim();
    if (!token) return;
    setSearchKey(provider, '');
    const command = provider === 'brave' ? 'brave_login' : 'image_search_login';
    const args = provider === 'brave' ? {token} : {provider, token};
    const restart = provider === 'brave' ? undefined : ' Reinicie os serviços para aplicar.';
    const result = await run<WebSearchStatus>(command, args, {success: {title: `Chave do ${provider === 'brave' ? 'Brave Search' : provider === 'unsplash' ? 'Unsplash' : 'Pexels'} validada e salva`, description: restart}, failure: 'A chave não foi aceita'});
    if (result) setWebStatus(result.value);
  }
  async function logoutSearch(provider: SearchKeyProvider) {
    const command = provider === 'brave' ? 'brave_logout' : 'image_search_logout';
    const args = provider === 'brave' ? {} : {provider};
    const result = await run<WebSearchStatus>(command, args, {success: 'Chave removida', failure: 'Não foi possível remover a chave'});
    if (result) setWebStatus(result.value);
  }
  function openTokens(url: string) {
    void run('open_url', {url}, {failure: 'Não foi possível abrir o endereço'});
  }

  return <div className="settings-grid">
    <div className="settings-credentials">
      <CredentialCard provider="hf" />
      {state?.models.some(model => model.source_url?.includes('civitai.com')) ? <CredentialCard provider="civitai" /> : null}
      <WebSearchCard status={webStatus} checked={webChecked} address={searxngAddress} keys={searchKeys} busy={busy} locked={locked} pending={pending} working={working} setAddress={setSearxngAddress} setKey={setSearchKey} refresh={() => void refreshWebSearch()} configure={enabled => void configureWebSearch(enabled)} saveAddress={saveSearchAddress} login={(provider, event) => void loginSearch(provider, event)} logout={provider => void logoutSearch(provider)} openUrl={openTokens} />
    </div>
    <div className="settings-side">
      <section className="card">
        <h3>Aparência</h3>
        <p className="muted small">“Sistema” acompanha o tema claro ou escuro do Windows.</p>
        <div className="theme-options" role="radiogroup" aria-label="Tema da interface">{themes.map(item => <label className={`theme-option ${theme === item.value ? 'selected' : ''}`} key={item.value}><input type="radio" name="theme" value={item.value} checked={theme === item.value} onChange={() => setTheme(item.value)} /><item.Icon size={20} aria-hidden="true" /><span>{item.label}</span></label>)}</div>
      </section>
      <section className="card">
        <div className="inline"><FileText size={20} className="text-accent" aria-hidden="true" /><h3>Sobre o aplicativo</h3></div>
        <dl className="review-list">
          <div><dt>Produto</dt><dd translate="no">{state?.product}</dd></div>
          <div><dt>Versão</dt><dd className="tabular">{state ? `v${state.version}` : '—'}</dd></div>
          <div><dt>Interface</dt><dd>Desktop</dd></div>
        </dl>
        <p className="muted small">{state?.settings.services_lifetime}</p>
        <div className="notice"><Shield size={17} aria-hidden="true" /><span>A interface se comunica apenas com o serviço local autenticado. Downloads e autenticação podem acessar serviços externos.</span></div>
      </section>
    </div>
  </div>;
}
