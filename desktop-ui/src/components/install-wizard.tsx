import { useState, type CSSProperties } from 'react';
import { ArrowLeft, ArrowRight, Check, CheckCircle2, CircleAlert, Download, ExternalLink, FolderOpen, HardDrive, ListChecks, Monitor, PackageCheck } from 'lucide-react';
import { command, blockingUnsupported, type DownloadAuth, type Estimate, type GitInstallResult, type InstallPlanReview, type Selection } from '../lib/api';
import { useDesktop } from '../lib/desktop';
import { bytes, cn, label, profileCompatibility, versionLabel } from '../lib/utils';
import { Badge, Button, ErrorNote, Modal } from './ui';
import { DownloadCredentialsDialog } from './download-credentials';

const locations = [
  {id: 'default', title: 'Padrão', text: 'Pasta recomendada pelo aplicativo.'},
  {id: 'portable', title: 'Portátil', text: 'Junto à distribuição do aplicativo.'},
  {id: 'custom', title: 'Personalizado', text: 'Escolha uma pasta em outro local ou unidade.'},
] as const;
const GIT_DOWNLOAD_URL = 'https://git-scm.com/download/win';

function PlanSteps({review}: {review: InstallPlanReview}) {
  const unsupported = new Map(blockingUnsupported(review).map(item => [item.step.sequence, item]));
  // A dry run cannot see what earlier steps will download, so steps that depend on them
  // report "not installed" here. Only steps this build cannot execute block the install.
  const pendingResults = review.results.filter(item => !item.ok);
  const pendingActions = review.action_results.filter(item => !item.ok);
  const blocked = unsupported.size > 0;
  return <details className="plan-review" open={blocked}>
    <summary><ListChecks size={16} aria-hidden="true" />Plano de execução · {review.plan.steps.length} etapas{blocked ? ' · requer atenção' : ''}</summary>
    <p className="small muted">Simulação real, sem downloads nem alterações no disco. Etapas que dependem de downloads anteriores aparecem como pendentes.</p>
    <ol className="plan-steps">
      {review.plan.steps.map(step => <li key={step.sequence}>
        <span className={cn('plan-marker', unsupported.has(step.sequence) && 'text-danger')} aria-hidden="true">{unsupported.has(step.sequence) ? <CircleAlert size={14} /> : <Check size={14} />}</span>
        <div className="grow"><strong>{step.summary}</strong><p className="small muted"><code>{step.action}</code> · <span className="path">{step.target}</span></p>
          {step.action === 'check' ? <p className="small muted">Feita no início da instalação, antes de qualquer download.</p> : null}
          {unsupported.has(step.sequence) ? <p className="small text-danger">{unsupported.get(step.sequence)?.reason || 'Esta etapa não pode ser executada por esta distribuição.'}</p> : null}
        </div>
      </li>)}
    </ol>
    {pendingResults.map((item, index) => <div className="notice" key={`result-${index}`}><span><strong>{item.target}</strong>: {item.failure?.message || item.detail || item.outcome}{item.failure?.detail ? ` — ${item.failure.detail}` : ''}</span></div>)}
    {pendingActions.map((item, index) => <div className="notice" key={`action-${index}`}><span><strong>{item.target}</strong> ({item.action}): {item.detail || 'Esta ação depende de etapas anteriores.'}</span></div>)}
  </details>;
}

export function InstallWizard({open, onOpenChange, initialProfile, onSubmitted}: {open: boolean; onOpenChange: (value: boolean) => void; initialProfile?: string; onSubmitted: (selection: Selection) => void}) {
  const {state, run, busy, pending, working, error, dismissError} = useDesktop();
  const [step, setStepValue] = useState(0);
  // 'none' on the first step: the dialog's own entrance is the only animation when it opens.
  const [direction, setDirection] = useState<'none' | 'forward' | 'back'>('none');
  function setStep(next: number) { setDirection(next > step ? 'forward' : 'back'); setStepValue(next); }
  const [profileId, setProfileId] = useState(initialProfile || state?.profiles.find(item => item.standing === 'recommended')?.id || state?.profiles[0]?.id || '');
  const [mode, setMode] = useState<Selection['mode']>('default');
  const [root, setRoot] = useState('');
  const [createSubfolder, setCreateSubfolder] = useState(true);
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [plan, setPlan] = useState<InstallPlanReview | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [downloadAuth, setDownloadAuth] = useState<DownloadAuth | null>(null);
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const [continueAfterCredentials, setContinueAfterCredentials] = useState(false);
  const [gitDialog, setGitDialog] = useState(false);
  const [gitResult, setGitResult] = useState<GitInstallResult | null>(null);
  const profile = state?.profiles.find(item => item.id === profileId);
  const selection: Selection = {profile_id: profileId, mode, ...(mode === 'custom' ? {custom_root: root.trim(), create_subfolder: createSubfolder} : {})};
  const gitMissing = Boolean(state?.profiles.some(item => item.blockers.includes('git_missing')));
  async function chooseFolder() {
    const current = root.trim();
    const result = await run<{path: string | null}>('choose_folder', /^(?:[A-Za-z]:[\\/]|\\\\)/.test(current) ? {initial_path: current} : {}, {inline: true});
    if (result?.value.path) setRoot(result.value.path);
  }
  async function review() {
    setReviewing(true);
    try {
      const estimated = await run<Estimate>('estimate', selection, {inline: true});
      if (!estimated) return;
      const planned = await run<InstallPlanReview>('plan', selection, {inline: true});
      if (!planned) return;
      const auth = await run<DownloadAuth>('download_auth', {profile_id: profileId}, {inline: true});
      setDownloadAuth(auth?.value ?? null);
      setEstimate(estimated.value); setPlan(planned.value); setStep(2);
    } finally { setReviewing(false); }
  }
  async function install() {
    onSubmitted(selection);
    const result = await run('install', selection, {inline: true, success: {title: 'Instalação concluída', description: profile?.name}, failure: 'A instalação falhou'});
    if (result) onOpenChange(false);
  }
  async function requestInstall() {
    const auth = await run<DownloadAuth>('download_auth', {profile_id: profileId}, {inline: true});
    setDownloadAuth(auth?.value ?? null);
    if (!auth || auth.value.providers.some(item => item.required && !item.configured)) {
      setContinueAfterCredentials(true);
      setCredentialsOpen(true);
      return;
    }
    await install();
  }
  function changeCredentialsOpen(value: boolean) {
    setCredentialsOpen(value);
    if (!value && !continueAfterCredentials) {
      void command<DownloadAuth>('download_auth', {profile_id: profileId}).then(setDownloadAuth).catch(() => setDownloadAuth(null));
    }
  }
  function openGitInstaller() {
    dismissError();
    setGitResult(null);
    setGitDialog(true);
  }
  async function installGit() {
    const result = await run<GitInstallResult>(
      'install_git',
      {confirmation: 'install-git'},
      {inline: true, failure: 'Não foi possível instalar o Git'},
    );
    if (result) setGitResult(result.value);
  }
  const blocked = estimate?.fits === false || Boolean(plan && blockingUnsupported(plan).length);
  return <>
  <Modal open={open} onOpenChange={value => { if (!pending && !reviewing) onOpenChange(value); }} title="Nova instalação" description="Escolha um perfil, defina o local e revise antes de instalar." className="wizard">
    <ol className="wizard-steps" aria-label="Etapas de instalação" style={{'--progress': (step + 1) / 3} as CSSProperties}>
      {['Perfil', 'Local', 'Revisão'].map((name, index) => <li key={name} className={cn(index === step && 'current', index < step && 'complete')} aria-current={index === step ? 'step' : undefined}><span>{index < step ? <Check size={14} aria-hidden="true" /> : index + 1}</span>{name}</li>)}
    </ol>
    <div className="wizard-body">
      <div className="wizard-step" key={step} data-direction={direction}>
      {step === 0 ? <>
        <div className="section-heading"><div><h3>Perfil</h3><p className="muted small">A compatibilidade considera a VRAM detectada. Nenhum download começa agora.</p></div><Monitor size={24} className="text-accent" aria-hidden="true" /></div>
        {gitMissing ? <div className="notice"><CircleAlert size={18} aria-hidden="true" /><span className="grow"><strong>Git for Windows é necessário</strong><br /><span className="small">O Hub usa Git para instalar o ComfyUI. Instale pelo Windows Package Manager ou baixe manualmente.</span></span><Button size="sm" disabled={busy} onClick={openGitInstaller}>Instalar Git</Button></div> : null}
        <div className="profile-options" role="radiogroup" aria-label="Perfil de instalação">
          {state?.profiles.map(item => <label key={item.id} className={cn('profile-option', profileId === item.id && 'selected', item.compatible === false && 'incompatible')}>
            <input type="radio" name="profile" value={item.id} checked={profileId === item.id} disabled={item.compatible === false || busy} onChange={() => setProfileId(item.id)} />
            <div className="grow">
              <div className="split"><span className="inline"><strong>{item.name}</strong>{item.version ? <Badge>{versionLabel(item.version)}</Badge> : null}</span><Badge tone={item.standing === 'recommended' ? 'accent' : item.compatible === false ? 'warning' : 'neutral'}>{profileCompatibility(item)}</Badge></div>
              <p className="muted small">{item.summary}</p>
              <span className="small">{item.required_vram_gb != null ? `${item.required_vram_gb} GB de VRAM` : 'VRAM não informada'}</span>
            </div>
          </label>)}
        </div>
        {!state?.profiles.length ? <ErrorNote message="Nenhum perfil está disponível nesta distribuição. Reinstale o aplicativo com seus manifests." /> : null}
      </> : null}
      {step === 1 ? <>
        <h3>Local da instalação</h3>
        <p className="muted">Modelos, ambientes e configurações ficam na pasta da instalação.</p>
        <div className="location-options" role="radiogroup" aria-label="Local da instalação">
          {locations.map(item => <label className={cn('location-option', mode === item.id && 'selected')} key={item.id}><input name="location-mode" type="radio" value={item.id} checked={mode === item.id} disabled={busy} onChange={() => setMode(item.id)} /><span><strong>{item.title}</strong><span className="muted small">{item.text}</span></span></label>)}
        </div>
        {mode === 'custom' ? <div className="field">
          <label htmlFor="install-root">{createSubfolder ? 'Pasta principal' : 'Pasta de instalação'}</label>
          <div className="input-action">
            <input id="install-root" name="install-root" value={root} onChange={event => setRoot(event.target.value)} placeholder="D:\InteligenciaArtificial\Pepito…" spellCheck={false} autoComplete="off" list="recent-roots" disabled={busy || reviewing} />
            <Button variant="secondary" onClick={() => void chooseFolder()} disabled={busy || reviewing} busy={pending === 'choose_folder'} aria-label="Escolher pasta de instalação"><FolderOpen size={17} aria-hidden="true" /></Button>
          </div>
          <datalist id="recent-roots">{state?.settings.recent_custom_roots.map(path => <option key={path} value={path} />)}</datalist>
          <p className="field-hint">Você também pode colar ou editar o caminho completo.</p>
          <button type="button" role="switch" aria-checked={createSubfolder} aria-describedby="install-subfolder-hint" className={cn('switch', createSubfolder && 'switch-on')} disabled={busy || reviewing} onClick={() => setCreateSubfolder(value => !value)}>
            <span className="switch-thumb" aria-hidden="true" /><span className="switch-label">Criar pasta com o nome da instalação</span>
          </button>
          <p id="install-subfolder-hint" className="field-hint">{createSubfolder ? <>Cria a pasta <strong>{profile?.name}</strong> dentro do local escolhido. Todos os arquivos da instalação ficam nela.</> : 'Usa exatamente a pasta escolhida, sem criar uma subpasta.'}</p>
        </div> : null}
        <div className="notice"><HardDrive size={18} aria-hidden="true" /><span>Na próxima etapa, consultamos o espaço livre, o volume conhecido dos downloads e simulamos o plano de instalação.</span></div>
      </> : null}
      {step === 2 && estimate && plan ? <>
        <div className="review-icon"><PackageCheck size={30} aria-hidden="true" /></div>
        <h3>Revisar e instalar {profile?.name}</h3>
        <p className="muted">O aplicativo baixa os componentes reais e prepara os serviços locais. Mantenha esta janela aberta durante a operação.</p>
        <dl className="review-list">
          <div><dt>Perfil</dt><dd>{profile?.name}{profile?.version ? ` ${versionLabel(profile.version)}` : ''}</dd></div>
          <div><dt>Modo</dt><dd>{label(mode)}</dd></div>
          <div><dt>Destino</dt><dd className="path">{plan.plan.install_root || estimate.root || (mode === 'custom' ? root : mode === 'portable' ? 'Pasta portátil da distribuição' : 'Pasta padrão do aplicativo')}</dd></div>
          <div><dt>Download {estimate.complete ? 'estimado' : 'mínimo conhecido'}</dt><dd>{bytes(estimate.known_bytes)}</dd></div>
          <div><dt>Espaço livre</dt><dd>{bytes(estimate.free_bytes)}</dd></div>
        </dl>
        {estimate.fits === false ? <ErrorNote message="O espaço livre é insuficiente para os arquivos conhecidos. Volte e escolha outra unidade." /> : <div className="notice"><CheckCircle2 size={18} aria-hidden="true" /><span>{estimate.fits === true ? 'Os downloads conhecidos cabem nesta unidade.' : 'Não foi possível confirmar o espaço disponível.'} Ambientes e extrações podem exigir espaço adicional.{!estimate.complete ? ' Alguns componentes não informam o tamanho; a estimativa é parcial.' : ''}</span></div>}
        {downloadAuth?.providers.some(item => item.required && !item.configured) ? <div className="notice" role="status"><CircleAlert size={18} aria-hidden="true" /><span><strong>Token do Civitai não configurado.</strong><br />Os arquivos do Civitai deste perfil exigem autenticação para novos downloads. Configure aqui ou continue sem token se já tiver os arquivos no disco.</span></div> : null}
        {downloadAuth?.providers.some(item => item.provider === 'hf') ? <p className="small muted">Os arquivos atuais do Hugging Face são públicos. O token é opcional; repositórios com acesso restrito podem exigir aceitar a licença.</p> : null}
        {downloadAuth === null || downloadAuth.providers.length ? <div className="actions"><Button variant="secondary" disabled={busy} onClick={() => { setContinueAfterCredentials(false); setCredentialsOpen(true); }}>Configurar tokens de download</Button></div> : null}
        {plan && blockingUnsupported(plan).length ? <ErrorNote message="Esta distribuição não consegue executar todas as etapas do plano. Revise os detalhes abaixo." /> : null}
        <PlanSteps review={plan} />
      </> : null}
      </div>
      <ErrorNote message={error} />
    </div>
    <div className="dialog-footer">
      <Button variant="ghost" disabled={busy || reviewing} onClick={() => step > 0 ? setStep(step - 1) : onOpenChange(false)}><ArrowLeft size={16} aria-hidden="true" />{step === 0 ? 'Cancelar' : 'Voltar'}</Button>
      {step === 0 ? <Button onClick={() => setStep(1)} disabled={busy || !profile || profile.compatible === false}>Escolher local<ArrowRight size={16} aria-hidden="true" /></Button>
        : step === 1 ? <Button onClick={() => void review()} busy={reviewing} busyLabel="Revisando…" disabled={busy || reviewing || (mode === 'custom' && !root.trim())}>Revisar instalação<ArrowRight size={16} aria-hidden="true" /></Button>
        : <Button onClick={() => void requestInstall()} busy={working(['install', 'download_auth'])} busyLabel="Preparando instalação…" disabled={busy || blocked}><Download size={16} aria-hidden="true" />Instalar perfil</Button>}
    </div>
  </Modal>
  <DownloadCredentialsDialog profileId={profileId} open={open && credentialsOpen} onOpenChange={changeCredentialsOpen} onContinue={continueAfterCredentials ? () => void install() : undefined} />
  <Modal open={gitDialog} onOpenChange={value => { if (!value && !working('install_git')) { setGitDialog(false); dismissError(); } }}
    title="Instalar Git for Windows"
    description="O Hub executará o pacote oficial Git.Git pelo Windows Package Manager (winget). O Windows pode pedir autorização.">
    {gitResult
      ? <div className="notice"><CheckCircle2 size={18} aria-hidden="true" /><span>{gitResult.message}</span></div>
      : <div className="notice"><CircleAlert size={18} aria-hidden="true" /><span>Nenhum modelo ou componente da stack será baixado agora. Depois da instalação, feche e abra o Hub para atualizar a verificação.</span></div>}
    <ErrorNote message={error} />
    <div className="dialog-footer">
      <Button variant="ghost" disabled={busy} busy={working('open_url', {url: GIT_DOWNLOAD_URL})} busyLabel="Abrindo…" onClick={() => void run('open_url', {url: GIT_DOWNLOAD_URL}, {inline: true, failure: 'Não foi possível abrir o site do Git'})}><ExternalLink size={16} aria-hidden="true" />Baixar manualmente</Button>
      <Button variant="secondary" disabled={working('install_git')} onClick={() => { setGitDialog(false); dismissError(); }}>{gitResult ? 'Fechar' : 'Cancelar'}</Button>
      {!gitResult ? <Button disabled={busy} busy={working('install_git')} busyLabel="Instalando Git…" onClick={() => void installGit()}><Download size={16} aria-hidden="true" />Instalar com winget</Button> : null}
    </div>
  </Modal>
  </>;
}
