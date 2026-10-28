import { useCallback, useEffect, useRef, useState } from 'react';
import { erc20Abi } from 'viem';
import { useAccount, useReadContract, useSwitchChain, useWriteContract } from 'wagmi';
import { ConnectButton } from '@rainbow-me/rainbowkit';
import { CheckCircle2, Loader2, RefreshCw, Send, Wallet, XCircle } from 'lucide-react';
import { robinhood } from '../lib/wallet';

const short = a => a ? `${a.slice(0, 6)}…${a.slice(-4)}` : '—';
const OPEN = new Set(['pending', 'approved', 'sending']);
const rejected = e => e?.code === 4001 || e?.name === 'UserRejectedRequestError' || /reject|denied|cancel/i.test(e?.shortMessage || e?.message || '');
const fmt = v => v == null ? '—' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 });
const explorer = tx => `https://robinhoodchain.blockscout.com/tx/${tx}`;

export const AdminClaims = ({ api, token, settings, onChange }) => {
  const { address, chainId, isConnected } = useAccount();
  const { switchChainAsync } = useSwitchChain();
  const { writeContractAsync } = useWriteContract();
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(null);
  const [msg, setMsg] = useState(null);
  const [filter, setFilter] = useState('open');
  const resumed = useRef(new Set());
  const decimals = settings?.decimals ?? 18;
  const { data: raw, refetch } = useReadContract({
    address: settings?.tokenAddress, abi: erc20Abi, functionName: 'balanceOf', args: [address], chainId: robinhood.id,
    query: { enabled: !!(address && settings?.tokenAddress), refetchInterval: 15000 },
  });
  const balance = raw === undefined ? null : Number(raw) / 10 ** decimals;
  const wrongNetwork = isConnected && chainId !== robinhood.id;

  const load = useCallback(async () => {
    try { const d = await api('/claims', { token }); setData(d); onChange?.(d); } catch (e) { setMsg({ type: 'err', text: e.message }); }
  }, [api, token, onChange]);
  useEffect(() => { load(); const timer = setInterval(load, 20000); return () => clearInterval(timer); }, [load]);

  const confirm = useCallback(async (claimId, txHash) => {
    for (let attempt = 0; attempt < 60; attempt++) {
      const res = await api(`/claims/${claimId}/confirm`, { method: 'POST', token, body: { txHash } });
      if (res.status === 'sent') return res.claim;
      await new Promise(r => setTimeout(r, 3000));
    }
    throw new Error('The transfer is still pending on Robinhood Chain. It will be re-checked automatically.');
  }, [api, token]);

  useEffect(() => {
    for (const c of data?.claims || []) {
      if (c.status === 'sending' && c.tx_hash && !resumed.current.has(c.claim_id)) {
        resumed.current.add(c.claim_id);
        confirm(c.claim_id, c.tx_hash).then(() => load()).catch(e => setMsg({ type: 'err', text: e.message }));
      }
    }
  }, [data, confirm, load]);

  const payout = async (c) => {
    setMsg(null);
    if (!isConnected) { setMsg({ type: 'err', text: 'Connect the MetaMask wallet that will send the tokens first.' }); return; }
    setBusy(c.claim_id);
    try {
      if (wrongNetwork) await switchChainAsync({ chainId: robinhood.id });
      const { transfer } = await api(`/claims/${c.claim_id}/approve`, { method: 'POST', token, body: { payer: address } });
      if (raw != null && BigInt(raw) < BigInt(transfer.amount)) {
        await load();
        setMsg({ type: 'err', text: `Claim approved, but ${short(address)} only holds ${fmt(balance)} ${transfer.symbol} and the player needs ${fmt(c.amount_tokens)}. Top up this wallet, then press Send tokens.` });
        return;
      }
      let txHash;
      try {
        txHash = await writeContractAsync({ address: transfer.tokenAddress, abi: erc20Abi, functionName: 'transfer', args: [transfer.to, BigInt(transfer.amount)], chainId: robinhood.id });
      } catch (e) {
        await load();
        setMsg({ type: 'err', text: rejected(e) ? 'Transfer cancelled in your wallet. The claim stays approved — press “Send tokens” to try again.' : (e.shortMessage || e.message) });
        return;
      }
      setMsg({ type: 'ok', text: `Transfer sent (${short(txHash)}). Waiting for Robinhood Chain to confirm…` });
      await load();
      const done = await confirm(c.claim_id, txHash);
      setMsg({ type: 'ok', text: `Paid ${fmt(done.amount_tokens)} ${done.symbol} to ${short(done.wallet_address)} · tx ${short(txHash)}.` });
      refetch();
    } catch (e) { setMsg({ type: 'err', text: e.message }); }
    finally { setBusy(null); load(); }
  };

  const decline = async (c) => {
    const reason = window.prompt(`Decline the claim from ${c.username || short(c.wallet_address)}? Their $${c.usd.toFixed(4)} goes back to their balance and the 24h timer resets.\n\nReason (optional):`);
    if (reason === null) return;
    setBusy(c.claim_id); setMsg(null);
    try { await api(`/claims/${c.claim_id}/reject`, { method: 'POST', token, body: { reason } }); setMsg({ type: 'ok', text: 'Claim declined and the balance was returned to the player.' }); }
    catch (e) { setMsg({ type: 'err', text: e.message }); }
    finally { setBusy(null); load(); }
  };

  const rows = (data?.claims || []).filter(c => filter === 'all' || OPEN.has(c.status));
  return <div className="admin-card" data-testid="admin-claims-card">
    <div className="admin-card-head"><h2>Claim requests</h2>
      <i className={`admin-badge ${data?.open ? 'warn' : ''}`} data-testid="admin-claims-open-badge">{data ? `${data.open} waiting · ${data.sentCount} paid` : '…'}</i></div>
    <p className="admin-hint">Players can request a payout once every 24 hours. Approve a request to send the tokens from the wallet connected below — MetaMask will ask you to confirm the transfer, and the claim is marked paid once the transaction confirms on-chain.</p>
    <div className="admin-payer" data-testid="admin-payer">
      <div className="admin-payer-wallet"><Wallet size={16}/><ConnectButton chainStatus="icon" showBalance={false} accountStatus="address"/></div>
      {isConnected && <span className="admin-payer-balance" data-testid="admin-payer-balance">{wrongNetwork ? <i className="admin-badge warn">switch to Robinhood Chain</i> : <>Payer holds <strong>{fmt(balance)} {settings?.symbol}</strong></>}</span>}
      <div className="admin-actions" style={{ marginTop: 0, marginLeft: 'auto' }}>
        <select className="admin-filter" value={filter} onChange={e => setFilter(e.target.value)} data-testid="admin-claims-filter" style={{ minWidth: 0 }}><option value="open">Open requests</option><option value="all">All claims</option></select>
        <button type="button" className="admin-btn ghost" onClick={load} data-testid="admin-claims-refresh-button"><RefreshCw size={14}/></button>
      </div>
    </div>
    {msg && <div className={`admin-msg ${msg.type}`} style={{ marginTop: 12 }} data-testid="admin-claims-message">{msg.text}</div>}
    {rows.length ? <div className="admin-scroll"><table className="admin-table" data-testid="admin-claims-table"><thead><tr><th>When</th><th>Player</th><th>USD</th><th>Tokens</th><th>Status</th><th/></tr></thead><tbody>
      {rows.map(c => <tr key={c.claim_id} data-testid={`admin-claim-row-${c.claim_id}`} className={`claim-${c.status}`}>
        <td>{new Date(c.created_at).toLocaleString()}</td>
        <td><strong>{c.username || 'Islander'}</strong><br/><span className="mono" title={c.wallet_address}>{short(c.wallet_address)}</span></td>
        <td>${c.usd.toFixed(4)}</td>
        <td>{fmt(c.amount_tokens)} {c.symbol}</td>
        <td data-testid={`admin-claim-status-${c.claim_id}`}><span className={`claim-pill claim-${c.status}`}>{c.status}</span>{c.tx_hash && <><br/><a href={explorer(c.tx_hash)} target="_blank" rel="noreferrer">{short(c.tx_hash)}</a></>}{c.error && <><br/><small style={{ color: '#ffb4b4' }}>{c.error}</small></>}{c.reject_reason && <><br/><small>{c.reject_reason}</small></>}</td>
        <td style={{ whiteSpace: 'nowrap' }}>{OPEN.has(c.status) && c.status !== 'sending' && <>
          <button type="button" className="admin-btn" disabled={busy === c.claim_id} onClick={() => payout(c)} data-testid={`admin-claim-approve-${c.claim_id}`}>{busy === c.claim_id ? <Loader2 size={14} className="spin"/> : c.status === 'approved' ? <Send size={14}/> : <CheckCircle2 size={14}/>} {c.status === 'approved' ? 'Send tokens' : 'Approve & send'}</button>
          <button type="button" className="admin-btn ghost" style={{ marginLeft: 6 }} disabled={busy === c.claim_id} onClick={() => decline(c)} data-testid={`admin-claim-reject-${c.claim_id}`}><XCircle size={14}/> Decline</button>
        </>}{c.status === 'sending' && <span className="admin-badge"><Loader2 size={12} className="spin"/> confirming</span>}</td>
      </tr>)}
    </tbody></table></div> : <p className="admin-hint" style={{ marginTop: 12 }} data-testid="admin-claims-empty">{data ? (filter === 'open' ? 'No claims are waiting for approval.' : 'No claims yet.') : 'Loading claims…'}</p>}
  </div>;
};
