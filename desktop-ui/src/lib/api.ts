export interface Profile {
  id: string; name: string; summary: string; compatible: boolean | null;
  standing: string | null; required_vram_gb: number | null; blockers: string[];
  /** The profile version this Hub ships. */
  version: string | null;
}
export interface GitInstallResult {
  installed: boolean; changed: boolean; restart_required: boolean; message: string;
}
export interface InstallationEventRecord { at: string; result: string; summary: string }
export interface Installation {
  root: string; profile_id: string | null; profile_name: string | null;
  mode: string; status: string; active: boolean; last_used_at: string | null; detail: string;
  last_install?: InstallationEventRecord | null; last_repair?: InstallationEventRecord | null; last_failure?: InstallationEventRecord | null;
  /** What the last successful install or repair applied; null when unknown. */
  profile_version: string | null;
  /** What this Hub ships of the same profile; null for a profile it does not know. */
  hub_profile_version: string | null;
}
export interface ActivityDetail { category: string; action: string; item: string; detail?: string | null; elapsed_seconds?: number | null }
export interface OperationEvent {
  operation_id: string; sequence: number; kind: string; state: string; timestamp: string;
  phase: string; message: string; activity?: ActivityDetail | null; detail?: ActivityDetail | null;
  progress?: { completed: number; total: number | null } | null;
  transfer?: { completed_bytes: number; total_bytes: number | null; bytes_per_second: number | null } | null;
  diagnostic?: { code: string; message: string; [key: string]: unknown } | null;
  result?: { state: string; message?: string; [key: string]: unknown } | null;
}
export interface InstallationControl {
  operation_id: string;
  state: 'running' | 'pausing' | 'paused' | 'cancelling';
  can_pause: boolean;
  can_cancel: boolean;
}
export interface RetryableInstallation {
  operation_id: string;
  profile_id: string;
  root: string;
  kind: 'install' | 'repair';
}
export interface Operation {
  started_at: string; last_event: OperationEvent; activity: ActivityDetail | null;
  last_progress: { completed: number; total: number | null } | null;
  last_transfer: { completed_bytes: number; total_bytes: number | null; bytes_per_second: number | null } | null;
}
export interface Conflict {
  preflight_id: string; disposition: string; available_policies: string[];
  conflicts: { service_id: string; reason: string; can_stop_safely: boolean; endpoint: {host: string; port: number}; managed_endpoint: {host: string; port: number} | null }[];
  hard_blockers: {code: string; message: string}[];
}
export interface Model { id: string; provider: string; kind: string; revision: string | null; profile_ids: string[]; source_url?: string | null; size_bytes?: number | null }
export interface ChatModelOption { id: string; reference: string; display_name: string; uncensored: boolean; imported: boolean; default: boolean; selected: boolean; size_bytes: number | null }
export interface ChatModels { selected: string; default: string; options: ChatModelOption[] }
export interface DesktopState {
  product: string; version: string; busy: boolean; conflict: Conflict | null;
  installation: { ready: boolean; profile_id: string | null; mode: string | null; root_display: string | null; checking: boolean };
  installations: Installation[]; profiles: Profile[];
  services: { services: {service_id: string; health: string}[]; observed_at: string };
  vram: { total_mib: number | null; free_mib: number | null; observed_at: string };
  operation: Operation | null; events: OperationEvent[]; models: Model[]; chat: ChatModels | null;
  installation_control: InstallationControl | null;
  retryable_installation: RetryableInstallation | null;
  settings: { recent_custom_roots: string[]; services_lifetime: string };
}
export interface Selection { profile_id: string; mode: 'default' | 'portable' | 'custom'; custom_root?: string; create_subfolder?: boolean }
export interface Estimate { known_bytes: number; complete: boolean; free_bytes: number | null; fits: boolean | null; root?: string }
export interface DeletePlan {root: string; profile_name: string; active: boolean; must_stop: boolean; refusal: string | null}
export interface SwitchPlan {root: string; profile_id: string | null; mode: string; must_stop: boolean; running_profile: string | null; problems: string[]}
export interface AuthStatus { authenticated: boolean; source?: string; username?: string | null; detail?: string }
export interface DownloadAuthProvider {
  provider: 'hf' | 'civitai'; name: string; required: boolean; configured: boolean; model_ids: string[];
}
export interface DownloadAuth { providers: DownloadAuthProvider[] }
export interface WebSearchStatus {
  enabled: boolean;
  engine: 'duckduckgo' | 'searxng' | 'brave';
  searxng_url: string;
  brave_configured: boolean;
  unsplash_configured: boolean;
  pexels_configured: boolean;
  brave_source?: string | null;
  unsplash_source?: string | null;
  pexels_source?: string | null;
}
export interface PlanStep { sequence: number; action: string; target: string; summary: string }
export interface PlanResolution { step: PlanStep; supported: boolean; component_id: string | null; reason: string | null }
/** The system check runs as the installation's preflight, not as an executor step. */
export const blockingUnsupported = (review: {unsupported: PlanResolution[]}) => review.unsupported.filter(item => item.step.action !== 'check');
export interface InstallPlanReview {
  ok: boolean; dry_run: true;
  plan: { profile_id: string; steps: PlanStep[]; dry_run: boolean; stack_id?: string; install_mode?: string; install_root?: string };
  resolution: PlanResolution[]; unsupported: PlanResolution[];
  results: {target: string; outcome: string; ok: boolean; detail: string | null; failure: {reason: string; message: string; detail: string | null} | null}[];
  action_results: {action: string; target: string; ok: boolean; detail: string | null}[];
}
export interface LogFile { service: string; name: string; size_bytes: number; modified_at: string }
export interface LogsResult { root: string; files: LogFile[]; service: string | null; lines: string[]; truncated: boolean }
export interface MeasureResult { root: string; bytes: number; files: number; partial: boolean; elapsed_ms: number }
export interface ServiceTarget { service_id: string; label: string; url: string | null; health: string; openable: boolean }

export class ApiError extends Error {
  constructor(message: string, public code = 'request_failed', public details?: unknown) { super(message); }
}
const fragment = new URLSearchParams(window.location.hash.slice(1));
const suppliedToken = fragment.get('token');
if (suppliedToken) {
  sessionStorage.setItem('pepitos.desktop.session', suppliedToken);
  history.replaceState(null, '', window.location.pathname + window.location.search);
}
const token = sessionStorage.getItem('pepitos.desktop.session');
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  if (!token) throw new ApiError('Esta janela não tem uma sessão autorizada. Feche-a e abra o aplicativo novamente.', 'unauthorized');
  let response: Response;
  try { response = await fetch(path, { ...init, headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', ...init?.headers }, cache: 'no-store' }); }
  catch (error) { if (error instanceof DOMException && error.name === 'AbortError') throw error; throw new ApiError('Não foi possível conectar ao serviço local. Verifique se o aplicativo continua aberto.', 'offline'); }
  const body = await response.json().catch(() => null);
  if (!body) throw new ApiError(`O serviço local respondeu de forma inesperada (HTTP ${response.status}). Feche e abra o aplicativo novamente.`, 'invalid_response');
  if (!response.ok || body.ok === false) throw new ApiError(body.error || `O serviço não conseguiu concluir a solicitação (HTTP ${response.status}).`, body.code, body.details);
  return body as T;
}
export const getState = (signal?: AbortSignal) => request<DesktopState>('/api/state', {signal});
export async function command<T = unknown>(name: string, args: object = {}): Promise<T> {
  const response = await request<{ok: true; result: T}>('/api/command', { method: 'POST', body: JSON.stringify({command: name, args}) });
  return response.result;
}
