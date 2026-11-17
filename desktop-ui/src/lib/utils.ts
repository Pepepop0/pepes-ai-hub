import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import type { Installation, Profile } from './api';

export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs));
const numbers = new Intl.NumberFormat('pt-BR', {maximumFractionDigits: 1});
export const number = (value: number) => numbers.format(value);
export function bytes(value?: number | null): string {
  if (value == null) return 'Não informado';
  if (value >= 1024 ** 3) return `${number(value / 1024 ** 3)} GB`;
  if (value >= 1024 ** 2) return `${number(value / 1024 ** 2)} MB`;
  if (value >= 1024) return `${number(value / 1024)} KB`;
  return `${number(value)} B`;
}
const dates = new Intl.DateTimeFormat('pt-BR', {dateStyle: 'short', timeStyle: 'short'});
export const date = (value?: string | null) => value && !Number.isNaN(Date.parse(value)) ? dates.format(new Date(value)) : 'Ainda não registrado';
const labels: Record<string, string> = {
  ready: 'Pronto', stopped: 'Parado', running: 'Em execução', missing: 'Pasta não encontrada', unreadable: 'Configuração ilegível', unknown: 'Estado desconhecido',
  up: 'Disponível', down: 'Indisponível', degraded: 'Instável',
  failed: 'Falhou', succeeded: 'Concluído', preparing: 'Preparando', queued: 'Na fila', finalizing: 'Finalizando',
  default: 'Padrão', portable: 'Portátil', custom: 'Personalizado',
  preparation: 'Preparação', 'resource-release': 'Liberação de recursos', 'service-start': 'Inicialização', submission: 'Envio', execution: 'Execução', finalization: 'Finalização',
  chat: 'Conversa', diffusion: 'Imagem', 'text-encoder': 'Codificador de texto', vae: 'VAE', upscaler: 'Ampliação',
  recommended: 'Recomendado', compatible: 'Compatível', incompatible: 'Requisito ausente',
  insufficient: 'VRAM insuficiente', unavailable: 'Requisito ausente',
};
export const label = (value?: string | null) => value ? labels[value.toLowerCase()] || value : 'Não informado';
const blockerLabels: Record<string, string> = {
  git_missing: 'Git não encontrado',
  nvidia_gpu_missing: 'GPU NVIDIA não detectada',
  os_mismatch: 'Windows obrigatório',
  python_unsupported: 'Python 3.11+ obrigatório',
  vram_below_minimum: 'VRAM insuficiente',
};
export function profileCompatibility(profile: Profile): string {
  if (profile.compatible === null) return 'Compatibilidade não confirmada';
  if (profile.compatible) return label(profile.standing || 'compatible');
  return blockerLabels[profile.blockers?.[0]] || label(profile.standing || 'unavailable');
}
/** Service health is the domain ServiceState (`UP`, `DOWN`, `DEGRADED`, `UNKNOWN`). */
export const isRunning = (health?: string) => health?.toUpperCase() === 'UP';

/**
 * How an installation row reads. Only the active installation is probed; for the others the
 * backend checks folder and configuration, so their `ready` means available, not verified.
 */
export function installationStatus(item: Installation): {label: string; tone: 'neutral' | 'success' | 'warning'; live?: boolean} {
  if (item.status === 'missing' || item.status === 'unreadable') return {label: label(item.status), tone: 'warning'};
  if (!item.active) return {label: 'Disponível', tone: 'neutral'};
  if (item.status === 'running') return {label: 'Em execução', tone: 'success', live: true};
  if (item.status === 'ready') return {label: 'Pronto', tone: 'success'};
  return {label: label(item.status), tone: 'neutral'};
}

export const versionLabel = (version?: string | null) => version ? `v${version}` : null;

/**
 * When the profile version an installation runs is not the one this Hub ships: what to do.
 * Only the installation in use can be repaired, so another one is pointed there first.
 */
export function profileUpdateHint(item: Installation): string | null {
  const hub = item.hub_profile_version;
  if (!hub || item.profile_version === hub || item.status === 'missing' || item.status === 'unreadable') return null;
  const how = item.active ? 'use Reparar para atualizar' : 'use esta instalação e Reparar para atualizar';
  return item.profile_version ? `Hub traz v${hub}: ${how}.` : `Versão instalada desconhecida. Hub traz v${hub}: ${how}.`;
}

/** Last two folders of a path, for narrow places; the full path goes in a title. */
export function shortPath(path: string) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  const separator = path.includes('\\') ? '\\' : '/';
  return parts.length <= 3 ? path : `…${separator}${parts.slice(-2).join(separator)}`;
}
