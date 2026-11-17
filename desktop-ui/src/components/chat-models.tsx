import { useState } from 'react';
import { Download, Eye, EyeOff, FileInput, MessageSquare, Trash2 } from 'lucide-react';
import type { ChatModelOption, ChatModels } from '../lib/api';
import { useDesktop } from '../lib/desktop';
import { bytes, cn } from '../lib/utils';
import { Badge, Button, Empty, ErrorNote, Modal } from './ui';

const EXAMPLES = ['qwen3:8b', 'hf.co/usuario/repositorio-GGUF:Q4_K_M'];

/** The curated unfiltered option and the default it switches back to. */
function switchTargets(chat: ChatModels) {
  const uncensored = chat.options.find(item => item.uncensored && !item.imported);
  const censored = chat.options.find(item => !item.uncensored && item.default)
    ?? chat.options.find(item => !item.uncensored && !item.imported);
  const current = chat.options.find(item => item.selected);
  return {uncensored, censored, current, on: Boolean(current?.uncensored)};
}

/** Confirmation shared by the switch and the list: switching downloads and reconfigures. */
function useChoose() {
  const {run, busy, working, error, dismissError} = useDesktop();
  const [target, setTarget] = useState<ChatModelOption | null>(null);
  async function choose() {
    if (!target) return;
    const result = await run('choose_chat_model', {model_id: target.id}, {
      inline: true,
      success: {title: 'Modelo de chat trocado', description: target.display_name},
      failure: 'Não foi possível trocar o modelo de chat',
    });
    if (result) setTarget(null);
  }
  const dialog = <Modal open={target !== null} onOpenChange={open => { if (!open && !working('choose_chat_model')) { setTarget(null); dismissError(); } }}
    title={target?.uncensored ? 'Usar modelo sem filtros' : 'Trocar modelo de chat'}
    description="O Hub baixa o modelo se ainda não estiver na instalação e passa a usá-lo no Open WebUI. Os serviços terminam como estavam.">
    {target ? <div className="path-box"><strong translate="no">{target.display_name}</strong><p>{target.size_bytes ? `Download de até ${bytes(target.size_bytes)}` : 'Tamanho informado pelo Ollama durante o download'}</p></div> : null}
    {target?.uncensored ? <div className="notice">Este modelo não foi treinado para recusar pedidos e responde ao que outros modelos recusam. As respostas são de sua responsabilidade.</div> : null}
    <ErrorNote message={error} />
    <div className="dialog-footer">
      <Button variant="secondary" disabled={working('choose_chat_model')} onClick={() => setTarget(null)}>Cancelar</Button>
      <Button disabled={busy} busy={working('choose_chat_model')} busyLabel="Trocando modelo…" onClick={() => void choose()}><Download size={16} aria-hidden="true" />Usar este modelo</Button>
    </div>
  </Modal>;
  return {ask: (option: ChatModelOption) => { dismissError(); setTarget(option); }, dialog};
}

/** Compact switch for the overview: unfiltered chat on or off. */
export function UncensoredSwitch() {
  const {state, busy, working} = useDesktop();
  const {ask, dialog} = useChoose();
  const chat = state?.chat;
  if (!chat) return null;
  const {uncensored, censored, current, on} = switchTargets(chat);
  if (!uncensored) return null;
  const next = on ? censored : uncensored;
  return <div className="chat-switch">
    <div className="grow">
      <span className="small muted">Modelo de chat</span>
      <p className="chat-switch-model" translate="no" title={current?.display_name}>{current?.display_name ?? chat.selected}</p>
    </div>
    <button type="button" role="switch" aria-checked={on} className={cn('switch', on && 'switch-on')} disabled={busy || !next} aria-busy={working('choose_chat_model') || undefined} onClick={() => next && ask(next)}>
      <span className="switch-thumb" aria-hidden="true" />
      <span className="switch-label">{on ? <EyeOff size={14} aria-hidden="true" /> : <Eye size={14} aria-hidden="true" />}Sem filtros</span>
    </button>
    {dialog}
  </div>;
}

/** Every chat model the installation in use can run, plus importing one. */
export function ChatModelsPanel({onNavigate}: {onNavigate: (page: string) => void}) {
  const {state, run, busy, working, error, dismissError} = useDesktop();
  const {ask, dialog} = useChoose();
  const [importing, setImporting] = useState(false);
  const [reference, setReference] = useState('');
  const chat = state?.chat;
  const active = state?.installations.find(item => item.active);
  if (!chat || !active) return <div className="card"><Empty icon={<MessageSquare size={28} />} title="Nenhuma instalação em uso" description="Escolha ou crie uma instalação para trocar o modelo de chat dela."><Button variant="secondary" onClick={() => onNavigate('installations')}>Abrir instalações</Button></Empty></div>;
  if (active.status === 'missing' || active.status === 'unreadable') return <div className="card"><Empty icon={<MessageSquare size={28} />} title="A pasta da instalação em uso não foi encontrada" description={`O modelo de chat é baixado dentro da pasta da instalação (${active.root}). Localize a pasta movida ou escolha outra instalação.`}><Button onClick={() => onNavigate('installations')}>Abrir instalações</Button></Empty></div>;
  async function submitImport() {
    const result = await run('import_chat_model', {reference}, {
      inline: true,
      success: {title: 'Modelo importado e em uso', description: reference.trim()},
      failure: 'Não foi possível importar o modelo',
    });
    if (result) { setImporting(false); setReference(''); }
  }
  return <>
    <div className="section-heading">
      <p className="muted">O modelo que responde no Open WebUI desta instalação. Trocar baixa o modelo se preciso; os modelos de imagem não mudam.</p>
      <Button variant="secondary" disabled={busy} onClick={() => { dismissError(); setImporting(true); }}><FileInput size={16} aria-hidden="true" />Importar modelo</Button>
    </div>
    <div className="chat-model-list">
      {chat.options.map(option => <article key={option.id} className={cn('card chat-model', option.selected && 'chat-model-selected')}>
        <div className="grow">
          <div className="inline wrap">
            <strong translate="no">{option.display_name}</strong>
            {option.selected ? <Badge tone="accent">Em uso</Badge> : null}
            {option.default ? <Badge>Padrão do perfil</Badge> : null}
            {option.uncensored ? <Badge tone="warning">Sem filtros</Badge> : null}
            {option.imported ? <Badge>Importado</Badge> : null}
          </div>
          <p className="small muted">{option.size_bytes ? bytes(option.size_bytes) : option.imported ? 'Tamanho conhecido após o download' : 'Tamanho não informado'}</p>
        </div>
        <div className="actions">
          {!option.selected ? <Button variant="secondary" size="sm" disabled={busy} onClick={() => ask(option)}>Usar este modelo</Button> : null}
          {option.imported && !option.selected ? <Button variant="ghost" size="sm" disabled={busy} busy={working('remove_imported_chat_model', {model_id: option.id})} busyLabel="Removendo…" aria-label={`Remover ${option.display_name}`} onClick={() => void run('remove_imported_chat_model', {model_id: option.id}, {success: {title: 'Modelo removido', description: option.display_name}, failure: 'Não foi possível remover o modelo'})}><Trash2 size={15} aria-hidden="true" />Remover</Button> : null}
        </div>
      </article>)}
    </div>
    {dialog}
    <Modal open={importing} onOpenChange={open => { if (!open && !working('import_chat_model')) setImporting(false); }} title="Importar modelo de chat"
      description="Cole o nome de um modelo do Ollama ou de um GGUF do Hugging Face. O Hub baixa e passa a usá-lo no Open WebUI desta instalação.">
      <div className="field">
        <label htmlFor="import-reference">Modelo</label>
        <input id="import-reference" name="import-reference" value={reference} onChange={event => setReference(event.target.value)} placeholder={`${EXAMPLES[0]}…`} autoComplete="off" spellCheck={false} disabled={working('import_chat_model')} />
        <p className="field-hint">Exemplos: {EXAMPLES.map((example, index) => <span key={example}>{index ? ' ou ' : ''}<code translate="no">{example}</code></span>)}. Inclua a tag (<code>:8b</code>, <code>:Q4_K_M</code>). Também aceita <code>ollama run …</code> e links do ollama.com ou huggingface.co.</p>
      </div>
      <div className="notice">O Hub não verifica modelos importados. Escolha um tamanho que caiba na VRAM junto com os modelos de imagem; se o download falhar, o modelo anterior continua em uso.</div>
      <ErrorNote message={error} />
      <div className="dialog-footer">
        <Button variant="secondary" disabled={working('import_chat_model')} onClick={() => setImporting(false)}>Cancelar</Button>
        <Button disabled={busy || !reference.trim()} busy={working('import_chat_model')} busyLabel="Importando…" onClick={() => void submitImport()}><Download size={16} aria-hidden="true" />Importar e usar</Button>
      </div>
    </Modal>
  </>;
}
