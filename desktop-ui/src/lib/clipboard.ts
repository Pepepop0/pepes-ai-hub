import { useCallback } from 'react';
import { useToast } from './toast';

/** Copies text and says so. Falls back to a hidden selection where the async clipboard is missing. */
export function useCopy() {
  const notify = useToast();
  return useCallback(async (text: string, what: string) => {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else {
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.append(area);
        area.select();
        const copied = document.execCommand('copy');
        area.remove();
        if (!copied) throw new Error('copy refused');
      }
      notify({tone: 'success', title: 'Copiado', description: what});
    } catch {
      notify({tone: 'danger', title: 'Não foi possível copiar', description: 'Selecione o texto e use Ctrl+C.'});
    }
  }, [notify]);
}
