import { useCallback, useEffect, useState } from 'react';
import { CircleAlert, KeyRound } from 'lucide-react';
import { command, type DownloadAuth } from '../lib/api';
import { useDesktop } from '../lib/desktop';
import { CredentialCard } from './credential-card';
import { Button, ErrorNote, Modal } from './ui';

interface DownloadCredentialsDialogProps {
  profileId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onContinue?: () => void;
}

function DownloadCredentialsContent({profileId, onOpenChange, onContinue}: Omit<DownloadCredentialsDialogProps, 'open'>) {
  const {pending} = useDesktop();
  const [auth, setAuth] = useState<DownloadAuth | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try { setAuth(await command<DownloadAuth>('download_auth', {profile_id: profileId})); }
    catch { setError('Não foi possível consultar as credenciais deste perfil. Tente novamente ou continue sabendo que um novo download pode exigir autenticação.'); }
    finally { setLoading(false); }
  }, [profileId]);
  useEffect(() => {
    let disposed = false;
    void command<DownloadAuth>('download_auth', {profile_id: profileId}).then(value => {
      if (!disposed) setAuth(value);
    }).catch(() => {
      if (!disposed) setError('Não foi possível consultar as credenciais deste perfil. Tente novamente ou continue sabendo que um novo download pode exigir autenticação.');
    }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [profileId]);
  const missing = auth?.providers.filter(item => item.required && !item.configured) ?? [];
  const hf = auth?.providers.find(item => item.provider === 'hf');
  const civitai = auth?.providers.find(item => item.provider === 'civitai');
  return <>
    {loading ? <p className="muted" role="status">Consultando credenciais locais…</p> : null}
    {missing.length ? <div className="notice" role="status"><CircleAlert size={18} aria-hidden="true" /><span><strong>Configure o token do {missing.map(item => item.name).join(', ')} antes de novos downloads.</strong><br />Sem ele, arquivos que exigem acesso autenticado podem falhar. Se já estão no disco, você pode continuar sem token para reutilizá-los.</span></div> : auth ? <p className="muted">{auth.providers.some(item => item.required) ? 'As credenciais necessárias estão configuradas localmente. Isso não confirma permissões de acesso nem aceitação de licenças.' : 'Este perfil não tem arquivos conhecidos que exijam token. Os arquivos públicos do Hugging Face continuam disponíveis sem autenticação.'}</p> : null}
    <ErrorNote message={error} />
    {error ? <Button variant="secondary" disabled={Boolean(pending)} onClick={() => void load()}>Consultar novamente</Button> : null}
    <div className="settings-credentials">
      {civitai ? <CredentialCard provider="civitai" configured={civitai.configured} onChanged={() => void load()} /> : null}
      <CredentialCard provider="hf" configured={hf?.configured} onChanged={() => void load()} />
    </div>
    <div className="dialog-footer wrap">
      <Button variant="secondary" disabled={Boolean(pending)} onClick={() => onOpenChange(false)}>Fechar</Button>
      {onContinue ? <Button disabled={Boolean(pending) || loading} onClick={() => { onOpenChange(false); onContinue(); }}><KeyRound size={16} aria-hidden="true" />{error ? 'Continuar sem conferir tokens' : missing.length ? 'Continuar sem token' : 'Continuar'}</Button> : null}
    </div>
  </>;
}

/** Closing unmounts every secret field; reopening starts with empty forms. */
export function DownloadCredentialsDialog({profileId, open, onOpenChange, onContinue}: DownloadCredentialsDialogProps) {
  return <Modal open={open} onOpenChange={onOpenChange} title="Credenciais para download" description="Configure e valide tokens aqui, sem sair da instalação. Tokens são protegidos pelo Windows; arquivos públicos não exigem autenticação.">
    {open ? <DownloadCredentialsContent key={profileId} profileId={profileId} onOpenChange={onOpenChange} onContinue={onContinue} /> : null}
  </Modal>;
}
