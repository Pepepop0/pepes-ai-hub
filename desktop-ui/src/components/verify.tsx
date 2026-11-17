import { useState } from 'react';
import { Wrench } from 'lucide-react';
import { useDesktop } from '../lib/desktop';
import { Badge, Button, ErrorNote, Modal } from './ui';

interface VerifyReport { problems: string[]; notes: string[]; ok: boolean }

/** Read-only verification of the installation in use, with its report dialog. */
export function useVerification() {
  const {run, busy, working, error} = useDesktop();
  const [report, setReport] = useState<VerifyReport | null>(null);
  const [open, setOpen] = useState(false);
  async function verify() {
    const result = await run<VerifyReport>('verify', {}, {failure: 'Não foi possível verificar a instalação'});
    if (result) { setReport(result.value); setOpen(true); }
  }
  async function repair() {
    const result = await run('repair', {}, {inline: true, success: 'Reparo concluído', failure: 'O reparo da instalação falhou'});
    if (result) setOpen(false);
  }
  const dialog = <Modal open={open} onOpenChange={setOpen} title="Verificação da instalação" description="Inspeção somente leitura dos arquivos e componentes. Nada foi alterado.">
    {report ? <>
      <Badge tone={report.ok ? 'success' : 'warning'}>{report.ok ? 'Nenhum problema detectado' : `${report.problems.length} problema(s) encontrado(s)`}</Badge>
      {report.problems.map((problem, index) => <p className="notice notice-danger" key={`problem-${index}`}>{problem}</p>)}
      {report.notes.map((note, index) => <p className="notice" key={`note-${index}`}>{note}</p>)}
      <ErrorNote message={error} />
      <div className="dialog-footer">
        <Button variant="secondary" onClick={() => setOpen(false)}>Fechar</Button>
        {!report.ok ? <Button disabled={busy} busy={working('repair')} busyLabel="Iniciando reparo…" onClick={() => void repair()}><Wrench size={16} aria-hidden="true" />Reparar instalação</Button> : null}
      </div>
    </> : null}
  </Modal>;
  return {verify, verifying: working('verify'), dialog};
}
