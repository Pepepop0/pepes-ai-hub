import type { JSX } from 'react';
import { Skeleton } from './ui';

function CardLines({lines = 3, title = true}: {lines?: number; title?: boolean}) {
  return <div className="card skeleton-card">
    {title ? <Skeleton className="skeleton-title" /> : null}
    {Array.from({length: lines}, (_, index) => <Skeleton key={index} className="skeleton-line" style={{width: `${92 - index * 14}%`}} />)}
  </div>;
}

function Overview() {
  return <>
    <div className="panel card skeleton-card"><Skeleton className="skeleton-icon" /><div className="grow"><Skeleton className="skeleton-heading" /><Skeleton className="skeleton-line" style={{width: '55%'}} /><div className="actions"><Skeleton className="skeleton-button" /><Skeleton className="skeleton-button" /></div></div></div>
    <div className="stats-grid">{[0, 1, 2].map(index => <div className="card stat" key={index}><Skeleton className="skeleton-line" style={{width: '45%'}} /><Skeleton className="skeleton-heading" style={{width: '60%'}} /><Skeleton className="skeleton-line" style={{width: '75%'}} /></div>)}</div>
    <div className="dashboard-grid"><CardLines lines={4} /><CardLines lines={3} /></div>
  </>;
}

function Installations() {
  return <div className="installation-list">{[0, 1, 2].map(index => <div className="card installation-card skeleton-card" key={index}>
    <Skeleton className="skeleton-icon" />
    <div className="grow"><Skeleton className="skeleton-title" style={{width: '30%'}} /><Skeleton className="skeleton-line" style={{width: '70%'}} /><Skeleton className="skeleton-line" style={{width: '40%'}} /><div className="actions"><Skeleton className="skeleton-button" /><Skeleton className="skeleton-button" /></div></div>
  </div>)}</div>;
}

function Models() {
  return <>
    <div className="tabs-list skeleton-tabs"><Skeleton className="skeleton-line" style={{width: 150}} /><Skeleton className="skeleton-line" style={{width: 150}} /></div>
    <div className="profiles-grid skeleton-grid">{[0, 1, 2].map(index => <CardLines key={index} lines={4} />)}</div>
  </>;
}

function Activity() {
  return <><CardLines lines={2} /><div className="card skeleton-card"><Skeleton className="skeleton-title" />{[0, 1, 2, 3].map(index => <div className="skeleton-row" key={index}><Skeleton className="skeleton-dot" /><div className="grow"><Skeleton className="skeleton-line" style={{width: '60%'}} /><Skeleton className="skeleton-line" style={{width: '35%'}} /></div></div>)}</div></>;
}

function Settings() {
  return <div className="settings-grid"><div className="settings-credentials"><CardLines lines={5} /><CardLines lines={5} /></div><div className="settings-side"><CardLines lines={2} /><CardLines lines={3} /></div></div>;
}

const pages: Record<string, () => JSX.Element> = {overview: Overview, installations: Installations, models: Models, activity: Activity, settings: Settings};

/** Layout-shaped placeholder shown until the first state arrives. */
export function PageSkeleton({page}: {page: string}) {
  const Page = pages[page] ?? Overview;
  return <div className="page-skeleton" role="status" aria-busy="true"><span className="sr-only">Carregando…</span><Page /></div>;
}
