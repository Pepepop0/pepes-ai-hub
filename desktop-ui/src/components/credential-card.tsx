import { useId, useState, type FormEvent } from 'react';
import { ExternalLink, KeyRound, LogOut, RefreshCw, Shield } from 'lucide-react';
import type { AuthStatus } from '../lib/api';
import { useDesktop } from '../lib/desktop';
import { Badge, Button, ErrorNote } from './ui';

const providers = {
  hf: {
    name: 'Hugging Face', description: 'Opcional para os arquivos públicos dos perfis atuais',
    explanation: 'Os arquivos atuais são públicos e podem ser baixados sem token. Se um repositório negar acesso ou exigir licença, aceite os termos na página do modelo com a mesma conta e use um token de leitura com acesso ao repositório.',
    placeholder: 'Token de leitura', tokensUrl: 'https://huggingface.co/settings/tokens', createLabel: 'Criar token de leitura',
  },
  civitai: {
    name: 'Civitai', description: 'Acesso autenticado para downloads hospedados no Civitai',
    explanation: 'Os arquivos hospedados no Civitai exigem acesso autenticado. Confira também as permissões de conteúdo da conta. Arquivos já disponíveis localmente podem ser reutilizados sem novo download.',
    placeholder: 'Chave API do Civitai', tokensUrl: 'https://civitai.com/user/account', createLabel: 'Criar chave API',
  },
} as const;

/** One credential convention for Settings and in-place download prompts. */
export function CredentialCard({provider, configured, onChanged}: {
  provider: 'hf' | 'civitai'; configured?: boolean; onChanged?: () => void;
}) {
  const info = providers[provider];
  const id = useId();
  const {run, busy, pending, working} = useDesktop();
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [token, setToken] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const statusCommand = `${provider}_status`;
  const loginCommand = `${provider}_login`;
  const logoutCommand = `${provider}_logout`;
  const locked = Boolean(pending);

  async function refresh() {
    setError(null);
    const result = await run<AuthStatus>(statusCommand, {}, {handle: () => {
      setError(`Não foi possível consultar o ${info.name}. Verifique a conexão e tente novamente.`); return true;
    }});
    if (result) setStatus(result.value);
  }
  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const secret = token.trim();
    setToken(''); setError(null); setSaved(false);
    if (!secret) return;
    const result = await run<AuthStatus>(loginCommand, {token: secret}, {handle: () => {
      setError(`Não foi possível validar e salvar o token do ${info.name}. Confira o token, a conexão e as permissões da conta. O armazenamento seguro requer Windows DPAPI.`); return true;
    }});
    if (result) { setStatus(result.value); setSaved(true); onChanged?.(); }
  }
  async function logout() {
    setToken(''); setError(null); setSaved(false);
    const result = await run<AuthStatus>(logoutCommand, {}, {handle: () => {
      setError('Não foi possível remover o token salvo. Tente novamente.'); return true;
    }});
    if (result) { setStatus(result.value); onChanged?.(); }
  }
  const authenticated = Boolean(status?.authenticated);
  return <section className="card">
    <div className="section-heading">
      <div className="inline"><div className="service-icon" aria-hidden="true"><KeyRound size={21} /></div><div><h3 translate="no">{info.name}</h3><p className="small muted">{info.description}</p></div></div>
      <Badge tone={authenticated ? 'success' : 'neutral'}>{pending === statusCommand ? 'Consultando…' : status ? authenticated ? 'Conectado' : 'Não conectado' : configured ? 'Token configurado' : 'Não consultado'}</Badge>
    </div>
    <p className="muted">{info.explanation}</p>
    {configured !== undefined ? <p className="small muted">{configured ? 'Há uma credencial local configurada. Isso não garante acesso a todos os modelos.' : 'Nenhuma credencial local legível está configurada.'}</p> : null}
    {status ? <div className="auth-status">
      <div className="split"><span>Conta</span><strong translate="no">{status.username || (authenticated ? 'Autenticada' : 'Nenhuma')}</strong></div>
      {status.source ? <p className="small muted">Origem da credencial: {status.source}</p> : null}
      {status.detail ? <p className="small muted">{status.detail}</p> : null}
    </div> : null}
    <form onSubmit={event => void login(event)}>
      <div className="field">
        <label htmlFor={`${id}-token`}>Token de acesso</label>
        <input id={`${id}-token`} name={`${provider}-token`} type="password" value={token} onChange={event => { setToken(event.target.value); setSaved(false); }} placeholder={info.placeholder} autoComplete="off" spellCheck={false} disabled={busy} aria-describedby={`${id}-token-hint`} />
        <p id={`${id}-token-hint`} className="field-hint">O token é validado e protegido pelo Windows (DPAPI). Ele não fica salvo nesta janela nem aparece em logs. O campo é limpo ao enviar e ao fechar.</p>
      </div>
      <ErrorNote message={error} />
      {saved ? <p className="small text-success" role="status">Token validado e salvo com proteção do Windows.</p> : null}
      <div className="actions">
        <Button type="submit" disabled={busy || !token.trim()} busy={pending === loginCommand} busyLabel="Validando…"><Shield size={16} aria-hidden="true" />Validar e salvar token</Button>
        <Button type="button" variant="secondary" size="icon" disabled={locked} onClick={() => void refresh()} aria-label={`Atualizar estado da autenticação no ${info.name}`} busy={pending === statusCommand}><RefreshCw size={16} aria-hidden="true" /></Button>
        {authenticated || configured ? <Button type="button" variant="ghost" disabled={busy} busy={pending === logoutCommand} busyLabel="Removendo…" onClick={() => void logout()}><LogOut size={16} aria-hidden="true" />Remover token salvo</Button> : null}
      </div>
    </form>
    <div className="card-bottom actions">
      <Button variant="ghost" size="sm" disabled={locked} busy={working('open_url', {url: info.tokensUrl})} busyLabel="Abrindo…" onClick={() => void run('open_url', {url: info.tokensUrl}, {handle: () => { setError('Não foi possível abrir a página de tokens. Verifique o navegador padrão.'); return true; }})}>{info.createLabel}<ExternalLink size={13} aria-hidden="true" /></Button>
    </div>
  </section>;
}
