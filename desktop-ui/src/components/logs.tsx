import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Copy, FileText, FolderOpen, Radio, RefreshCw } from 'lucide-react';
import { command, type LogsResult } from '../lib/api';
import { useCopy } from '../lib/clipboard';
import { useDesktop } from '../lib/desktop';
import { bytes, cn, date } from '../lib/utils';
import { Button, Empty, ErrorNote, Modal, Skeleton } from './ui';

const LINE_CHOICES = [200, 500, 1000];
const FOLLOW_MS = 2000;
// Colour codes some services write; the viewer shows plain text.
const ANSI = /\x1b\[[0-9;]*[A-Za-z]/g;

/** The tail of an installation's service logs. Mounted per opening (`key`) so state starts fresh. */
export function LogsDialog({root, name, open, onOpenChange, initialService = null}: {root: string; name: string; open: boolean; onOpenChange: (open: boolean) => void; initialService?: string | null}) {
  const {run, working} = useDesktop();
  const copy = useCopy();
  const [service, setService] = useState<string | null>(initialService);
  const [lines, setLines] = useState(LINE_CHOICES[0]);
  const [data, setData] = useState<LogsResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [follow, setFollow] = useState(false);
  const output = useRef<HTMLPreElement>(null);
  const atBottom = useRef(true);
  const inFlight = useRef(false);

  // Reads bypass the command queue: they never make other buttons wait.
  const load = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setLoading(true);
    try {
      setData(await command<LogsResult>('logs', service ? {root, service, lines} : {root, lines}));
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Não foi possível ler os logs.');
    } finally {
      inFlight.current = false;
      setLoading(false);
    }
  }, [root, service, lines]);

  useEffect(() => { if (open) void load(); }, [open, load]);
  useEffect(() => {
    if (!follow || !open) return;
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, FOLLOW_MS);
    return () => window.clearInterval(timer);
  }, [follow, open, load]);
  // New lines keep the view at the end, unless the reader scrolled up to read.
  useLayoutEffect(() => {
    const element = output.current;
    if (element && atBottom.current) element.scrollTop = element.scrollHeight;
  }, [data]);

  const current = service ?? data?.service ?? null;
  const text = data ? data.lines.join('\n').replace(ANSI, '') : '';
  const file = data?.files.find(item => item.service === current);
  return <Modal open={open} onOpenChange={onOpenChange} className="logs-dialog" title="Logs da instalação"
    description={<><strong>{name}</strong> · <span className="path" translate="no">{root}</span></>}>
    {data?.files.length ? <div className="logs-toolbar">
      <div className="segmented" role="group" aria-label="Arquivo de log">
        {data.files.map(item => <button type="button" key={item.service} className={cn('segment', item.service === current && 'segment-active')} aria-pressed={item.service === current} onClick={() => { atBottom.current = true; setService(item.service); }}>
          <span translate="no">{item.service}</span><span className="segment-meta tabular">{bytes(item.size_bytes)}</span>
        </button>)}
      </div>
      <div className="actions">
        <label className="sr-only" htmlFor="log-lines">Quantidade de linhas</label>
        <select id="log-lines" value={lines} onChange={event => setLines(Number(event.target.value))}>
          {LINE_CHOICES.map(choice => <option key={choice} value={choice}>Últimas {choice} linhas</option>)}
        </select>
        <Button variant={follow ? 'default' : 'secondary'} size="sm" aria-pressed={follow} onClick={() => { atBottom.current = true; setFollow(!follow); }}><Radio size={15} aria-hidden="true" />{follow ? 'Ao vivo' : 'Acompanhar'}</Button>
        <Button variant="secondary" size="icon" aria-label="Atualizar logs" busy={loading && !follow} onClick={() => void load()}><RefreshCw size={16} aria-hidden="true" /></Button>
        <Button variant="secondary" size="sm" disabled={!text} onClick={() => void copy(text, `${data.lines.length} linhas de ${current}.log`)}><Copy size={15} aria-hidden="true" />Copiar</Button>
      </div>
    </div> : null}
    <ErrorNote message={error} />
    {data === null && !error ? <div className="log-output log-loading" aria-hidden="true">{[90, 70, 82, 60, 76].map(width => <Skeleton key={width} className="skeleton-line" style={{width: `${width}%`}} />)}</div>
      : data && !data.files.length ? <Empty icon={<FileText size={28} />} title="Nenhum log nesta instalação" description="Os serviços escrevem seus logs nesta pasta quando são iniciados pelo Hub." />
      : data ? <pre ref={output} className="log-output" tabIndex={0} aria-label={`Últimas linhas de ${current}.log`} onScroll={event => { const element = event.currentTarget; atBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 24; }} translate="no">{text || 'O arquivo está vazio.'}</pre>
      : null}
    {file ? <p className="field-hint">{data?.truncated ? `Mostrando as últimas ${data.lines.length} linhas. ` : ''}Atualizado pelo serviço em {date(file.modified_at)}.</p> : null}
    <div className="dialog-footer">
      <Button variant="ghost" busy={working('open_folder', {root, kind: 'logs'})} busyLabel="Abrindo…" onClick={() => void run('open_folder', {root, kind: 'logs'}, {failure: 'Não foi possível abrir a pasta de logs'})}><FolderOpen size={16} aria-hidden="true" />Abrir pasta de logs</Button>
      <Button variant="secondary" onClick={() => onOpenChange(false)}>Fechar</Button>
    </div>
  </Modal>;
}
