import { useEffect, useState, type CSSProperties } from 'react';
import { ArrowRightLeft, Copy, FileText, FolderInput, FolderOpen, Gauge, HardDrive, Plus, Search, ShieldCheck, Trash2, Wrench } from 'lucide-react';
import { command, type ApiError, type DeletePlan, type Installation, type InstallationEventRecord, type MeasureResult, type SwitchPlan } from '../lib/api';
import { useCopy } from '../lib/clipboard';
import { useDesktop } from '../lib/desktop';
import { transitionName, useRetained } from '../lib/motion';
import { useToast } from '../lib/toast';
import { bytes, date, installationStatus, label, number, profileUpdateHint, versionLabel } from '../lib/utils';
import { ActionMenu, Badge, Button, Checkbox, Empty, ErrorNote, MenuItem, MenuSeparator, Modal } from './ui';
import { useVerification } from './verify';
import { OperationCard } from './operation';

/** Something another part of the app asked this page to start, e.g. from the command palette. */
export type InstallationRequest = {kind: 'register'} | {kind: 'switch'; root: string};
// Sizes measured this session, kept when the page is left and opened again.
const sizeCache = new Map<string, MeasureResult>();
// A filter only helps once the list stops fitting on one screen.
const FILTER_FROM = 4;
const statusFilters: Record<string, {label: string; test: (item: Installation) => boolean}> = {
  all: {label: 'Todos os estados', test: () => true},
  available: {label: 'Disponíveis', test: item => item.status !== 'missing' && item.status !== 'unreadable'},
  missing: {label: 'Pasta não encontrada ou ilegível', test: item => item.status === 'missing' || item.status === 'unreadable'},
};

type Action =
  | {kind: 'delete'; item: Installation; plan: DeletePlan}
  | {kind: 'switch'; item: Installation; plan: SwitchPlan}
  | {kind: 'forget' | 'relocate'; item: Installation}
  | {kind: 'register'};
const absolutePath = /^(?:[A-Za-z]:[\\/]|\\\\)/;
const titles: Record<Action['kind'], string> = {
  delete: 'Excluir instalação do disco', switch: 'Usar esta instalação', forget: 'Remover da lista',
  relocate: 'Localizar pasta movida', register: 'Registrar instalação existente',
};
const descriptions: Record<Action['kind'], string> = {
  delete: 'Esta ação é permanente. Modelos, ambientes, logs e configurações desta pasta serão apagados.',
  forget: 'Apenas o registro será removido. Os arquivos continuam no disco e podem ser registrados novamente.',
  relocate: 'Selecione o novo local de uma pasta que você já moveu. Esta ação não transfere arquivos.',
  switch: 'Nenhum serviço está em execução; a troca é imediata e não altera arquivos.',
  register: 'A pasta é validada antes de entrar na lista e passar a ser a instalação em uso.',
};
const submitLabels: Record<Action['kind'], [string, string]> = {
  delete: ['Excluir permanentemente', 'Excluindo…'], forget: ['Remover apenas da lista', 'Removendo…'],
  relocate: ['Atualizar localização', 'Atualizando…'], switch: ['Usar instalação', 'Trocando…'],
  register: ['Registrar instalação', 'Registrando…'],
};
const stopLabels: Partial<Record<Action['kind'], string>> = {
  switch: 'Parar serviços e usar instalação', relocate: 'Parar serviços e atualizar', register: 'Parar serviços e registrar',
};
const historyLabels = [['last_install', 'Instalação'], ['last_repair', 'Reparo'], ['last_failure', 'Última falha']] as const;
const displayName = (item: Installation) => item.profile_name || item.profile_id || 'Instalação registrada';

function History({item}: {item: Installation}) {
  const latestSuccess = Math.max(...[item.last_install, item.last_repair]
    .filter(record => record?.result === 'succeeded')
    .map(record => Date.parse(record!.at)));
  const previousFailure = item.last_failure && Date.parse(item.last_failure.at) <= latestSuccess
    ? item.last_failure : null;
  const entries = historyLabels.flatMap(([key, name]) => {
    const record: InstallationEventRecord | null | undefined = item[key];
    return record && !(key === 'last_failure' && previousFailure) ? [{name, record}] : [];
  });
  if (!entries.length && !previousFailure) return null;
  return <>
    <dl className="installation-history">{entries.map(({name, record}) => <div key={name}><dt className="sr-only">{name}</dt><dd className={name === 'Última falha' ? 'text-danger' : undefined}><strong>{name}:</strong> {record.summary || label(record.result)} · <time dateTime={record.at}>{date(record.at)}</time></dd></div>)}</dl>
    {previousFailure ? <details className="small muted"><summary>Falha anterior ao último sucesso (histórico)</summary><p>{previousFailure.summary || label(previousFailure.result)} · <time dateTime={previousFailure.at}>{date(previousFailure.at)}</time></p></details> : null}
  </>;
}

export function Installations({onInstall, onShowLogs, request, onRequestDone}: {onInstall: () => void; onShowLogs: (item: Installation) => void; request: InstallationRequest | null; onRequestDone: () => void}) {
  const {state, run, busy, pending, working, error, dismissError} = useDesktop();
  const copy = useCopy();
  const notify = useToast();
  const [sizes, setSizes] = useState<Record<string, MeasureResult>>(() => Object.fromEntries(sizeCache));
  const [measuring, setMeasuring] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [action, setAction] = useState<Action | null>(null);
  const shown = useRetained(action);
  const [path, setPath] = useState('');
  const [stop, setStop] = useState(false);
  // Set when the backend answered `stop_required`: consent is asked only then, naming what runs.
  const [stopPlan, setStopPlan] = useState<SwitchPlan | null>(null);
  const {verify, verifying, dialog: verifyDialog} = useVerification();
  const locked = Boolean(pending);
  function openAction(next: Action) { dismissError(); setAction(next); setPath(''); setStop(false); setStopPlan(null); }
  async function planDelete(item: Installation) {
    const result = await run<DeletePlan>('plan_delete', {root: item.root}, {failure: 'Não foi possível preparar a exclusão'});
    if (result) openAction({kind: 'delete', item, plan: result.value});
  }
  async function planSwitch(item: Installation) {
    const result = await run<SwitchPlan>('plan_switch', {root: item.root}, {failure: 'Não foi possível consultar a troca'});
    if (result) openAction({kind: 'switch', item, plan: result.value});
  }
  async function pickFolder() {
    const current = path.trim();
    const result = await run<{path: string | null}>('choose_folder', absolutePath.test(current) ? {initial_path: current} : {}, {inline: true});
    if (result?.value.path) setPath(result.value.path);
  }
  function stopRequired(failure: ApiError) {
    if (failure.code !== 'stop_required' || !failure.details) return false;
    setStopPlan(failure.details as SwitchPlan); setStop(false);
    return true;
  }
  async function submit() {
    if (!action) return;
    const consent = needsStop && stop;
    const result = action.kind === 'delete'
      ? await run('delete', {root: action.plan.root, confirmation: action.plan.root, stop: action.plan.must_stop}, {inline: true, reorder: true, success: {title: 'Instalação excluída', description: action.plan.root}, failure: 'A exclusão falhou'})
      : action.kind === 'switch'
        ? await run('switch', {root: action.item.root, stop: consent}, {inline: true, reorder: true, handle: stopRequired, success: {title: 'Instalação em uso', description: displayName(action.item)}, failure: 'A troca de instalação falhou'})
        : action.kind === 'forget'
          ? await run('forget', {root: action.item.root}, {inline: true, reorder: true, success: {title: 'Removida da lista', description: `Os arquivos continuam em ${action.item.root}.`}})
          : action.kind === 'relocate'
            ? await run('relocate', {old_root: action.item.root, new_root: path.trim(), stop: consent}, {inline: true, reorder: true, handle: stopRequired, success: {title: 'Localização atualizada', description: path.trim()}, failure: 'Não foi possível atualizar a localização'})
            : await run('register', {root: path.trim(), stop: consent}, {inline: true, reorder: true, handle: stopRequired, success: {title: 'Instalação registrada e em uso', description: path.trim()}, failure: 'Não foi possível registrar a instalação'});
    if (result) setAction(null);
  }
  const kind = shown?.kind;
  const refusal = shown?.kind === 'delete' ? shown.plan.refusal : shown?.kind === 'switch' && shown.plan.problems.length ? shown.plan.problems.join('\n') : null;
  const deleteNeedsStop = shown?.kind === 'delete' && shown.plan.must_stop;
  // A registered folder that is gone cannot be deleted; the useful next step is forgetting it.
  const deleteMissing = shown?.kind === 'delete' && shown.item.status === 'missing';
  const runningPlan = stopPlan ?? (shown?.kind === 'switch' && shown.plan.must_stop ? shown.plan : null);
  const needsStop = runningPlan !== null;
  const runningName = runningPlan ? state?.profiles.find(item => item.id === runningPlan.running_profile)?.name ?? runningPlan.running_profile ?? 'em uso' : null;
  const invalid = Boolean(refusal) || (needsStop && !stop)
    || ((kind === 'register' || kind === 'relocate') && !path.trim());
  const submitting = locked && pending !== 'choose_folder';
  const description = kind === 'switch' && runningPlan ? 'Os serviços em execução precisam ser parados antes da troca. Nenhum arquivo é alterado.' : kind ? descriptions[kind] : '';
  const [submitLabel, submitBusy] = kind ? submitLabels[kind] : ['', ''];
  useEffect(() => {
    if (!request) return;
    onRequestDone();
    if (request.kind === 'register') openAction({kind: 'register'});
    else {
      const item = state?.installations.find(entry => entry.root === request.root);
      if (item) void planSwitch(item);
    }
  // Runs once per request; the handlers it calls read the latest state.
  }, [request]);
  // Reads bypass the command queue: measuring a large folder never makes other buttons wait.
  async function measure(root: string) {
    setMeasuring(list => [...list, root]);
    try {
      const result = await command<MeasureResult>('measure', {root});
      sizeCache.set(root, result);
      setSizes(previous => ({...previous, [root]: result}));
    } catch (failure) {
      notify({tone: 'danger', title: 'Não foi possível calcular o tamanho', description: failure instanceof Error ? failure.message : undefined});
    } finally {
      setMeasuring(list => list.filter(item => item !== root));
    }
  }
  const words = query.trim().toLocaleLowerCase('pt-BR');
  const all = state?.installations ?? [];
  const filtering = all.length >= FILTER_FROM;
  const listed = filtering ? all.filter(item => statusFilters[statusFilter].test(item)
    && (!words || `${displayName(item)} ${item.root}`.toLocaleLowerCase('pt-BR').includes(words))) : all;
  return <>
    <div className="section-heading">
      <p className="muted">Cada instalação mantém seus próprios modelos e configurações.</p>
      <div className="actions">
        <Button variant="secondary" disabled={busy} onClick={() => openAction({kind: 'register'})}><FolderInput size={16} aria-hidden="true" />Registrar existente</Button>
        <Button disabled={busy} onClick={onInstall}><Plus size={16} aria-hidden="true" />Nova instalação</Button>
      </div>
    </div>
    <OperationCard operation={state?.operation ?? null} />
    {filtering ? <div className="filters installation-filters">
      <div className="search-field"><Search size={16} aria-hidden="true" /><label className="sr-only" htmlFor="installation-search">Buscar instalação</label><input id="installation-search" name="installation-search" type="search" placeholder="Buscar por nome ou pasta…" autoComplete="off" spellCheck={false} value={query} onChange={event => setQuery(event.target.value)} /></div>
      <label className="sr-only" htmlFor="installation-status">Estado</label>
      <select id="installation-status" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}>{Object.entries(statusFilters).map(([value, filter]) => <option key={value} value={value}>{filter.label}</option>)}</select>
    </div> : null}
    {!all.length ? <div className="card"><Empty icon={<HardDrive size={32} />} title="Nenhuma instalação registrada" description="Instale um perfil ou registre uma pasta que já contém uma instalação do Pepito’s AI Hub."><Button disabled={busy} onClick={onInstall}><Plus size={16} aria-hidden="true" />Criar primeira instalação</Button></Empty></div>
      : !listed.length ? <div className="card"><Empty icon={<Search size={28} />} title="Nenhuma instalação corresponde ao filtro" description={`${number(all.length)} instalações registradas. Ajuste a busca ou o estado.`}><Button variant="secondary" onClick={() => { setQuery(''); setStatusFilter('all'); }}>Limpar filtros</Button></Empty></div>
      : <div className="installation-list">{listed.map(item => {
        const unavailable = item.status === 'missing' || item.status === 'unreadable';
        const status = installationStatus(item);
        const updateHint = profileUpdateHint(item);
        const incomplete = item.active && !unavailable && item.status !== 'running' && state!.installation.profile_id !== null && !state!.installation.ready && !state!.installation.checking;
        const size = sizes[item.root];
        const sizing = measuring.includes(item.root);
        const deleting = working('delete', {root: item.root});
        const switching = working('switch', {root: item.root}) || working('relocate', {old_root: item.root});
        const menuBusy = working('plan_delete', {root: item.root}) ? 'Preparando…'
          : working('open_folder', {root: item.root, kind: 'logs'}) || working('open_folder', {root: item.root, kind: 'models'}) ? 'Abrindo…' : null;
        return <article className={`card installation-card ${item.active ? 'active-installation' : ''} ${deleting ? 'installation-leaving' : ''}`} key={item.root} style={{'--vt-name': transitionName('installation', item.root)} as CSSProperties} aria-busy={deleting || switching || undefined}>
          <div className="installation-icon" aria-hidden="true"><HardDrive size={22} /></div>
          <div className="grow">
            <div className="inline wrap">
              <h3>{displayName(item)}</h3>
              {item.profile_version ? <Badge>{versionLabel(item.profile_version)}</Badge> : null}
              {item.active ? <Badge tone="accent">Em uso</Badge> : null}
              {deleting ? <Badge tone="danger">Excluindo…</Badge> : switching ? <Badge tone="accent">Trocando…</Badge> : <Badge tone={status.tone} live={status.live}>{status.label}</Badge>}
              {incomplete ? <Badge tone="warning">Incompleta</Badge> : null}
            </div>
            <p className="installation-path-row"><span className="path installation-path" translate="no">{item.root}</span><Button variant="ghost" size="icon" className="copy-button" aria-label={`Copiar caminho de ${displayName(item)}`} onClick={() => void copy(item.root, item.root)}><Copy size={13} aria-hidden="true" /></Button></p>
            <p className="muted small">{label(item.mode)} · Último uso: {date(item.last_used_at)}
              {size ? <> · <span className="tabular" title={`${number(size.files)} arquivos`}>{bytes(size.bytes)}{size.partial ? ' (parcial)' : ''}</span></> : null}
              {!unavailable ? <Button variant="ghost" size="sm" className="inline-action" busy={sizing} busyLabel="Calculando…" onClick={() => void measure(item.root)}><Gauge size={13} aria-hidden="true" />{size ? 'Recalcular' : 'Calcular tamanho'}</Button> : null}
            </p>
            {item.detail ? <p className="small muted">{item.detail}</p> : null}
            {updateHint ? <p className="small muted">{updateHint}</p> : null}
            <History item={item} />
            <div className="actions installation-actions">
              <Button variant="secondary" size="sm" disabled={locked || item.status === 'missing'} busy={working('open_folder', {root: item.root, kind: 'root'})} busyLabel="Abrindo…" onClick={() => void run('open_folder', {root: item.root, kind: 'root'}, {failure: 'Não foi possível abrir a pasta'})}><FolderOpen size={15} aria-hidden="true" />Abrir pasta</Button>
              {item.status === 'missing' ? <Button variant="ghost" size="sm" disabled={busy} onClick={() => openAction({kind: 'relocate', item})}><FolderInput size={15} aria-hidden="true" />Localizar pasta movida</Button> : null}
              {item.active && !unavailable ? <Button variant="ghost" size="sm" onClick={() => onShowLogs(item)}><FileText size={15} aria-hidden="true" />Ver logs</Button> : null}
              {!item.active ? <Button variant="ghost" size="sm" disabled={busy || unavailable} busy={working('plan_switch', {root: item.root})} busyLabel="Consultando…" onClick={() => void planSwitch(item)}><ArrowRightLeft size={15} aria-hidden="true" />Usar instalação</Button>
                : <>
                  <Button variant="ghost" size="sm" disabled={busy || unavailable} busy={verifying} busyLabel="Verificando…" onClick={() => void verify()}><ShieldCheck size={15} aria-hidden="true" />Verificar (somente leitura)</Button>
                  <Button variant="ghost" size="sm" disabled={busy || unavailable} busy={working('repair')} busyLabel="Reparando…" onClick={() => void run('repair', {}, {success: 'Reparo concluído', failure: 'O reparo da instalação falhou'})}><Wrench size={15} aria-hidden="true" />Reparar</Button>
                </>}
            </div>
          </div>
          <ActionMenu label="Mais ações" busy={menuBusy !== null} busyLabel={menuBusy ?? undefined}>
            <MenuItem disabled={item.status === 'missing'} onSelect={() => onShowLogs(item)}><FileText size={14} aria-hidden="true" />Ver logs</MenuItem>
            <MenuItem disabled={locked || item.status === 'missing'} onSelect={() => void run('open_folder', {root: item.root, kind: 'logs'}, {failure: 'Não foi possível abrir a pasta de logs'})}>Abrir pasta de logs</MenuItem>
            <MenuItem disabled={locked || item.status === 'missing'} onSelect={() => void run('open_folder', {root: item.root, kind: 'models'}, {failure: 'Não foi possível abrir a pasta de modelos'})}>Abrir pasta de modelos</MenuItem>
            <MenuItem onSelect={() => void copy(item.root, item.root)}><Copy size={14} aria-hidden="true" />Copiar caminho</MenuItem>
            <MenuSeparator className="dropdown-separator" />
            <MenuItem disabled={busy} onSelect={() => openAction({kind: 'relocate', item})}>Localizar pasta movida</MenuItem>
            <MenuItem disabled={busy} onSelect={() => openAction({kind: 'forget', item})}>Remover da lista (manter arquivos)</MenuItem>
            <MenuSeparator className="dropdown-separator" />
            <MenuItem danger disabled={locked} onSelect={() => void planDelete(item)}><Trash2 size={14} aria-hidden="true" />Excluir do disco</MenuItem>
          </ActionMenu>
        </article>;
      })}</div>}
    <div className="notice"><FolderInput size={18} aria-hidden="true" /><span>Moveu uma pasta pelo Windows? Use <strong>Localizar pasta movida</strong> para atualizar o registro. Nenhum arquivo é movido por essa ação. <strong>Remover da lista</strong> mantém os arquivos; <strong>Excluir do disco</strong> apaga a pasta.</span></div>
    <Modal open={action !== null} onOpenChange={open => { if (!open && !locked) setAction(null); }} title={kind ? titles[kind] : ''} description={description}>
      {shown && 'item' in shown ? <div className="path-box"><strong>{displayName(shown.item)}</strong><p className="path" translate="no">{shown.item.root}</p></div> : null}
      {runningPlan ? <div className="notice">Os serviços do perfil <strong>{runningName}</strong> estão em execução. Para continuar, o Hub para esses serviços e depois ativa a instalação escolhida.</div> : null}
      <ErrorNote message={refusal} />
      {deleteNeedsStop ? <div className="notice">Os serviços desta instalação estão em execução e serão parados antes da exclusão.</div> : null}
      {shown?.kind === 'relocate' || shown?.kind === 'register' ? <div className="field">
        <label htmlFor="registration-path">{shown.kind === 'relocate' ? 'Novo local da instalação' : 'Pasta da instalação existente'}</label>
        <div className="input-action">
          <input id="registration-path" name="registration-path" autoComplete="off" spellCheck={false} value={path} onChange={event => { setPath(event.target.value); setStopPlan(null); setStop(false); }} placeholder="D:\InteligenciaArtificial\Pepito…" disabled={submitting} />
          <Button variant="secondary" disabled={locked} busy={pending === 'choose_folder'} onClick={() => void pickFolder()} aria-label="Escolher pasta"><FolderOpen size={17} aria-hidden="true" /></Button>
        </div>
        <p className="field-hint">Cole, edite ou escolha o caminho completo. Cancelar o seletor mantém o texto atual.</p>
      </div> : null}
      {runningPlan ? <Checkbox checked={stop} onCheckedChange={setStop} disabled={submitting}>Autorizo parar os serviços do perfil {runningName}. Conversas e tarefas em andamento serão interrompidas.</Checkbox> : null}
      <ErrorNote message={error} />
      <div className="dialog-footer">
        <Button variant="secondary" disabled={locked} onClick={() => setAction(null)}>Cancelar</Button>
        {deleteMissing && shown && 'item' in shown
          ? <Button disabled={busy} busy={working('forget', {root: shown.item.root})} busyLabel="Removendo…" onClick={() => void run('forget', {root: shown.item.root}, {inline: true, reorder: true, success: {title: 'Removida da lista', description: shown.item.root}}).then(result => { if (result) setAction(null); })}>Remover da lista</Button>
          : <Button variant={kind === 'delete' ? 'destructive' : 'default'} disabled={busy || invalid} busy={submitting} busyLabel={submitBusy} onClick={() => void submit()}>{kind === 'delete' ? <Trash2 size={16} aria-hidden="true" /> : null}{deleteNeedsStop ? 'Parar serviços e excluir' : runningPlan && kind ? stopLabels[kind] : submitLabel}</Button>}
      </div>
    </Modal>
    {verifyDialog}
  </>;
}
