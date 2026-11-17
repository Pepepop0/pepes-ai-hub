import { useState } from 'react';
import { CheckCircle2, CircleAlert, CircleStop, KeyRound, LoaderCircle, Pause, Play, RefreshCw } from 'lucide-react';
import type { DownloadAuth, InstallationControl, Operation, RetryableInstallation } from '../lib/api';
import { useDesktop } from '../lib/desktop';
import { bytes, label, number } from '../lib/utils';
import { Badge, Button, Modal, Progress } from './ui';
import { DownloadCredentialsDialog } from './download-credentials';

function duration(seconds: number) {
  const safe = Math.max(0, Math.floor(seconds));
  if (safe < 5) return 'agora';
  if (safe < 60) return `há ${safe} s`;
  const minutes = Math.floor(safe / 60);
  if (minutes < 60) return `há ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `há ${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
}

function processName(category: string, action: string, item: string) {
  if (category === 'dependency') return 'pip (Python)';
  if (category === 'environment') return 'Python';
  if (category === 'transfer' && ['cloning', 'receiving', 'resolving'].includes(action)) return 'Git';
  if (category === 'transfer' && action === 'downloading') return 'Download HTTP';
  if (category === 'model' && action === 'pulling') return 'Ollama';
  if (category === 'service' || category === 'readiness') return item;
  return null;
}

function activityMessage(message: string, activity: Operation['activity']) {
  if (!activity) return message;
  const verb: Record<string, string> = {
    checking: 'Verificando', locating: 'Localizando', preparing: 'Preparando',
    cloning: 'Clonando', receiving: 'Recebendo', resolving: 'Resolvendo',
    creating: 'Criando', reusing: 'Reutilizando', validating: 'Validando',
    installing: 'Instalando', finalizing: 'Finalizando', verifying: 'Verificando',
    downloading: 'Baixando', extracting: 'Extraindo', starting: 'Iniciando',
    waiting: 'Aguardando', pulling: 'Baixando', fetching: 'Atualizando', rebuilding: 'Recriando',
  };
  return verb[activity.action] ? `${verb[activity.action]} ${activity.item}` : message;
}

function InstallationControls({control}: {control: InstallationControl}) {
  const {run, pending} = useDesktop();
  const [confirmCancel, setConfirmCancel] = useState(false);
  const paused = control.state === 'paused';
  const cancelling = control.state === 'cancelling';
  const pausing = control.state === 'pausing';
  const args = {operation_id: control.operation_id};
  const message = paused
    ? 'Instalação pausada. Retome para continuar ou cancele.'
    : pausing ? 'Aguardando um ponto seguro para pausar. A etapa atual pode precisar terminar.'
      : cancelling ? 'Cancelamento solicitado. A etapa atual pode precisar terminar.'
        : 'A pausa é aplicada em um ponto seguro. Instaladores externos podem precisar concluir a etapa atual.';
  async function cancel() {
    const result = await run('cancel_installation', args, {failure: 'Não foi possível cancelar a instalação'});
    if (result) setConfirmCancel(false);
  }
  return <>
    <div className="installation-controls">
      <div className="inline">
        <Button variant="secondary" size="sm" disabled={Boolean(pending) || cancelling || pausing || (!paused && !control.can_pause)}
          busy={pending === 'pause_installation' || pending === 'resume_installation'}
          busyLabel={paused ? 'Retomando…' : 'Solicitando pausa…'}
          onClick={() => void run(paused ? 'resume_installation' : 'pause_installation', args, {failure: 'Não foi possível alterar a pausa'})}>
          {paused ? <Play size={15} aria-hidden="true" /> : <Pause size={15} aria-hidden="true" />}{paused ? 'Retomar' : pausing ? 'Pausando…' : 'Pausar'}
        </Button>
        <Button variant="ghost" size="sm" className="text-danger" disabled={Boolean(pending) || !control.can_cancel || cancelling}
          onClick={() => setConfirmCancel(true)}><CircleStop size={15} aria-hidden="true" />{cancelling ? 'Cancelando…' : 'Cancelar instalação'}</Button>
      </div>
      <p className="small muted" role="status">{message}</p>
    </div>
    <Modal open={confirmCancel} onOpenChange={value => { if (pending !== 'cancel_installation') setConfirmCancel(value); }}
      title="Cancelar instalação?"
      description="Os arquivos já concluídos serão mantidos. Você poderá tentar novamente ou usar Reparar; a etapa atual pode precisar terminar antes do cancelamento.">
      <div className="dialog-footer">
        <Button variant="secondary" disabled={pending === 'cancel_installation'} onClick={() => setConfirmCancel(false)}>Voltar</Button>
        <Button variant="destructive" busy={pending === 'cancel_installation'} busyLabel="Cancelando…" disabled={Boolean(pending)} onClick={() => void cancel()}>Cancelar instalação</Button>
      </div>
    </Modal>
  </>;
}

function RetryInstallation({attempt, authFailure}: {attempt: RetryableInstallation; authFailure: boolean}) {
  const {run, busy, pending, working} = useDesktop();
  const [credentialsOpen, setCredentialsOpen] = useState(false);
  const [continueAfterAuth, setContinueAfterAuth] = useState(false);
  async function retry() {
    await run('retry_installation', {operation_id: attempt.operation_id}, {
      success: 'Instalação concluída',
      failure: 'A nova tentativa não foi concluída',
    });
  }
  async function checkAndRetry() {
    const result = await run<DownloadAuth>('download_auth', {profile_id: attempt.profile_id}, {failure: 'Não foi possível conferir os tokens de download'});
    if (!result) return;
    if (authFailure || result.value.providers.some(provider => provider.required && !provider.configured)) {
      setContinueAfterAuth(true);
      setCredentialsOpen(true);
    } else await retry();
  }
  return <>
    <div className="installation-controls">
      <div className="inline">
        <Button size="sm" disabled={busy} busy={pending === 'download_auth' || working('retry_installation')} busyLabel="Preparando nova tentativa…" onClick={() => void checkAndRetry()}><RefreshCw size={15} aria-hidden="true" />Tentar novamente</Button>
        <Button variant="secondary" size="sm" disabled={busy} onClick={() => { setContinueAfterAuth(false); setCredentialsOpen(true); }}><KeyRound size={15} aria-hidden="true" />Configurar tokens</Button>
      </div>
      <p className="small muted">Repete o perfil e o destino desta tentativa, mesmo sem uma instalação ativa. Arquivos já verificados são reutilizados.</p>
      <p className="path small muted">{attempt.root}</p>
    </div>
    <DownloadCredentialsDialog profileId={attempt.profile_id} open={credentialsOpen} onOpenChange={setCredentialsOpen}
      onContinue={() => { setCredentialsOpen(false); if (continueAfterAuth) void retry(); }} />
  </>;
}

export function OperationCard({operation}: {operation: Operation | null}) {
  const {state} = useDesktop();
  if (!operation) return null;
  const event = operation.last_event;
  const terminal = event.state === 'succeeded' || event.state === 'failed';
  const control = !terminal && state?.installation_control?.operation_id === event.operation_id ? state.installation_control : null;
  const paused = control?.state === 'paused';
  const cancelled = event.diagnostic?.code === 'cancelled';
  const retry = event.state === 'failed' && state?.retryable_installation?.operation_id === event.operation_id ? state.retryable_installation : null;
  const status = cancelled ? 'Cancelada' : paused ? 'Pausada' : control?.state === 'pausing' ? 'Pausando' : control?.state === 'cancelling' ? 'Cancelando' : label(event.state);
  // Snapshot last_* fields are historical by contract. Only the latest event's
  // observations are current; reusing a tiny completed metadata download made
  // a later, silent pip install look frozen forever at 0%.
  const transfer = event.transfer ?? null;
  const progress = event.progress ?? null;
  const percent = transfer?.total_bytes
    ? transfer.completed_bytes / transfer.total_bytes * 100
    : progress?.total ? progress.completed / progress.total * 100 : null;
  const activity = operation.activity;
  const process = activity ? processName(activity.category, activity.action, activity.item) : null;
  const eventAge = duration((Date.now() - new Date(event.timestamp).getTime()) / 1000);
  const operationAge = duration((Date.now() - new Date(operation.started_at).getTime()) / 1000);
  const noPercentage = activity?.category === 'dependency' && activity.action === 'installing'
    ? 'O pip não informa percentual enquanto grava e organiza os pacotes.'
    : activity?.category === 'readiness' && activity.action === 'waiting'
      ? 'O serviço ainda não respondeu. O Hub aguarda até 10 minutos e falha logo se o processo fechar.'
      : 'Esta etapa não fornece um percentual.';

  return <section className="card operation-card" aria-label="Operação atual" data-paused={paused || undefined}>
    <div className="section-heading">
      <div className="inline">
        <span className={`operation-symbol ${event.state === 'failed' && !cancelled ? 'text-danger' : 'text-accent'}`} aria-hidden="true">
          {event.state === 'succeeded' ? <CheckCircle2 size={22} /> : cancelled ? <CircleStop size={22} /> : event.state === 'failed' ? <CircleAlert size={22} /> : paused ? <Pause size={22} /> : <LoaderCircle className="spin" size={22} />}
        </span>
        <div><h3>{status}</h3><p className="muted small">{label(event.phase)}</p></div>
      </div>
      <Badge tone={cancelled ? 'neutral' : event.state === 'failed' ? 'danger' : event.state === 'succeeded' ? 'success' : paused ? 'warning' : 'accent'}>{terminal ? 'Última operação' : paused ? 'Pausada' : 'Em andamento'}</Badge>
    </div>
    <p className="operation-message" role="status">{terminal ? event.message : activityMessage(event.message, activity)}</p>
    {activity ? <p className="muted small">{activity.item}{activity.detail ? ` · ${activity.detail}` : ''}</p> : null}
    {!terminal ? <>
      <div className="operation-context small muted">
        {process ? <span><strong>Subprocesso:</strong> {process}</span> : <span><strong>Etapa:</strong> processo interno do Hub</span>}
        <span><strong>Operação iniciada:</strong> {operationAge}</span>
        <span><strong>Última atualização:</strong> {eventAge}</span>
      </div>
      <Progress value={percent} label="Progresso da operação" />
      <div className="split small muted">
        <span>{transfer ? `${bytes(transfer.completed_bytes)} de ${bytes(transfer.total_bytes)}` : progress ? `${progress.completed} de ${progress.total ?? '?'} etapas` : noPercentage}</span>
        <span>{!paused && transfer?.bytes_per_second ? `${bytes(transfer.bytes_per_second)}/s · ` : ''}{percent !== null ? `${number(percent)}%` : paused ? 'Pausada' : 'Em execução'}</span>
      </div>
    </> : null}
    {control ? <InstallationControls key={control.operation_id} control={control} /> : null}
    {retry ? <RetryInstallation key={retry.operation_id} attempt={retry} authFailure={['model-download-http-401', 'model-download-http-403'].includes(event.diagnostic?.code ?? '')} /> : null}
    {event.diagnostic ? <details className="diagnostic"><summary>Detalhes do diagnóstico</summary><pre>{JSON.stringify(event.diagnostic, null, 2)}</pre></details> : null}
  </section>;
}
