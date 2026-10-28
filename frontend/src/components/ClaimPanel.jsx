import { useEffect, useState } from 'react';
import { Gift, Loader2, Hourglass, Clock, CheckCircle2, XCircle, Send } from 'lucide-react';
import { fmtUsd, fmtTokens } from '../hooks/useEconomy';

const pad = n => String(n).padStart(2, '0');
export const countdown = iso => {
  const ms = new Date(iso).getTime() - Date.now();
  if (!(ms > 0)) return null;
  const s = Math.floor(ms / 1000);
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
};

export function useCountdown(iso) {
  const [left, setLeft] = useState(() => iso ? countdown(iso) : null);
  useEffect(() => {
    if (!iso) { setLeft(null); return undefined; }
    setLeft(countdown(iso));
    const timer = setInterval(() => setLeft(countdown(iso)), 1000);
    return () => clearInterval(timer);
  }, [iso]);
  return left;
}

const PENDING_LABEL = { pending: 'waiting for admin approval', approved: 'approved · tokens on the way', sending: 'transfer confirming on-chain' };

export const ClaimButton = ({ game, config, onClaim, busy }) => {
  const pending = game?.pending_claim;
  const left = useCountdown(pending ? null : game?.next_claim_at);
  const claimable = game?.claimable_usd ?? 0;
  const min = config?.minClaimUsd ?? 0.05;
  if (pending) return <button className="game-btn btn-teal inv-claim" data-testid="inventory-claim-button" data-state="pending" disabled title={PENDING_LABEL[pending.status]}>
    <span className="btn-icon"><Hourglass size={18} className="pulse"/></span><span className="btn-label">PENDING<small data-testid="inventory-claim-pending">{fmtTokens(pending.amount_tokens)} {pending.symbol}</small></span>
  </button>;
  if (left) return <button className="game-btn btn-teal inv-claim" data-testid="inventory-claim-button" data-state="cooldown" disabled title="One claim per wallet every 24 hours">
    <span className="btn-icon"><Clock size={18}/></span><span className="btn-label">NEXT CLAIM<small data-testid="inventory-claim-countdown">in {left}</small></span>
  </button>;
  const ok = claimable >= min;
  return <button className="game-btn btn-gold inv-claim" data-testid="inventory-claim-button" data-state="ready" disabled={!ok || busy} onClick={onClaim} title={ok ? 'Request your payout' : `Earn at least ${fmtUsd(min)} first`}>
    <span className="btn-icon">{busy ? <Loader2 size={18} className="spin"/> : <Gift size={18}/>}</span><span className="btn-label">CLAIM<small>{ok ? 'once every 24h' : `min ${fmtUsd(min)}`}</small></span>
  </button>;
};

const ICON = { pending: Hourglass, approved: Send, sending: Loader2, sent: CheckCircle2, rejected: XCircle, failed: XCircle };
export const ClaimHistory = ({ load, revision }) => {
  const [data, setData] = useState(null);
  useEffect(() => { let on = true; load().then(d => { if (on) setData(d); }).catch(() => {}); return () => { on = false; }; }, [load, revision]);
  if (!data?.claims?.length) return null;
  return <div className="claim-history" data-testid="claim-history">
    <div className="ledger-heading"><span>CLAIM REQUESTS</span><span>one per wallet every {data.cooldownHours ?? 24}h · paid after admin approval</span></div>
    {data.claims.slice(0, 5).map(c => { const Icon = ICON[c.status] || Hourglass; return <div className={`claim-row claim-${c.status}`} key={c.claim_id} data-testid={`claim-row-${c.claim_id}`}>
      <span className="claim-icon"><Icon size={14} className={c.status === 'sending' ? 'spin' : ''}/></span>
      <div className="claim-main"><strong>{fmtTokens(c.amount_tokens)} {c.symbol}</strong><span>{fmtUsd(c.usd, 4)} · {new Date(c.created_at).toLocaleString('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })}</span></div>
      <span className="claim-status" data-testid={`claim-status-${c.claim_id}`}>{c.status === 'sent' && c.tx_hash ? <a href={`https://robinhoodchain.blockscout.com/tx/${c.tx_hash}`} target="_blank" rel="noreferrer">paid · tx ↗</a> : c.status === 'rejected' ? `declined${c.reject_reason ? ` · ${c.reject_reason}` : ''}` : PENDING_LABEL[c.status] || c.status}</span>
    </div>; })}
  </div>;
};
