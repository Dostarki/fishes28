"""Player claim requests: once per 24h per wallet, paid out manually by the admin from MetaMask."""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from db import db
from game import now_iso
from state import mutate_state

COOLDOWN = timedelta(hours=24)
MIN_CLAIM_USD = 0.05
OPEN = ('pending', 'approved', 'sending')


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def claim_view(doc):
    out = {k: v for k, v in doc.items() if k not in ('_id', 'session_id', 'micro')}
    for k in ('created_at', 'approved_at', 'sent_at', 'rejected_at', 'next_claim_at'):
        if isinstance(out.get(k), datetime):
            out[k] = iso(out[k])
    return out


def summary(doc):
    return {'claim_id': doc['claim_id'], 'usd': doc['usd'], 'amount_tokens': doc['amount_tokens'], 'symbol': doc['symbol'],
            'status': doc['status'], 'created_at': iso(doc['created_at'])}


async def request_claim(session, settings, meta, price):
    sid, wallet_address = session['session_id'], session['wallet_address']
    if await db.claims.find_one({'session_id': sid, 'status': {'$in': list(OPEN)}}):
        raise HTTPException(409, 'Your previous claim is still waiting for approval.')
    wallet = await db.wallets.find_one({'address': wallet_address}, {'_id': 0, 'next_claim_at': 1, 'username': 1}) or {}
    now = utcnow()
    if wallet.get('next_claim_at') and wallet['next_claim_at'] > now:
        raise HTTPException(429, f"You can claim once every 24 hours. Next claim at {iso(wallet['next_claim_at'])}.")
    next_at = now + COOLDOWN
    doc = {'claim_id': secrets.token_urlsafe(16), 'session_id': sid, 'wallet_address': wallet_address, 'username': wallet.get('username'),
           'token_usd': price['token_usd'], 'decimals': meta['decimals'], 'symbol': meta['symbol'],
           'token_address': settings['token_address'].lower(), 'status': 'pending', 'created_at': now, 'next_claim_at': next_at}
    taken = {}

    def take(state):
        if state.get('pending_claim'):
            raise HTTPException(409, 'Your previous claim is still waiting for approval.')
        micro = state['balance_micro']
        if micro < MIN_CLAIM_USD * 1_000_000:
            raise HTTPException(400, f'You need at least ${MIN_CLAIM_USD:.2f} in earnings to claim.')
        usd = micro / 1_000_000
        amount = int(round(usd / price['token_usd'] * 10 ** meta['decimals']))
        doc.update(usd=usd, amount=str(amount), amount_tokens=amount / 10 ** meta['decimals'])
        taken['micro'] = micro
        state['balance_micro'] = 0
        state['claimed_micro'] = state.get('claimed_micro', 0) + micro
        state['pending_claim'] = summary(doc)
        state['claim_cooldown_until'] = iso(next_at)
        state['events'].insert(0, {'text': f"Claim requested · ${usd:.4f} ≈ {doc['amount_tokens']:,.0f} {meta['symbol']}", 'kind': 'crew', 'at': now_iso()})

    state = await mutate_state(sid, take)
    doc['micro'] = taken['micro']
    await db.claims.insert_one(doc.copy())
    await db.wallets.update_one({'address': wallet_address}, {'$set': {'last_claim_at': now, 'next_claim_at': next_at}, '$inc': {'claim_count': 1}})
    return doc, state


async def get_claim(claim_id):
    doc = await db.claims.find_one({'claim_id': claim_id}, {'_id': 0})
    if not doc:
        raise HTTPException(404, 'Claim not found.')
    return doc


def set_pending_status(status):
    def apply(state):
        if state.get('pending_claim'):
            state['pending_claim']['status'] = status
    return apply


async def approve_claim(claim_id, payer):
    doc = await get_claim(claim_id)
    if doc['status'] not in ('pending', 'approved'):
        raise HTTPException(409, f"This claim is already {doc['status']}.")
    await db.claims.update_one({'claim_id': claim_id}, {'$set': {'status': 'approved', 'approved_at': utcnow(), 'payer': payer.lower(), 'error': None}})
    await mutate_state(doc['session_id'], set_pending_status('approved'))
    return await get_claim(claim_id)


async def reject_claim(claim_id, reason):
    doc = await get_claim(claim_id)
    if doc['status'] not in ('pending', 'approved'):
        raise HTTPException(409, f"This claim is already {doc['status']}.")
    result = await db.claims.update_one({'claim_id': claim_id, 'status': {'$in': ['pending', 'approved']}},
                                        {'$set': {'status': 'rejected', 'rejected_at': utcnow(), 'reject_reason': (reason or '').strip()[:200] or None}})
    if not result.modified_count:
        raise HTTPException(409, 'This claim was just updated. Refresh and try again.')

    def restore(state):
        state['balance_micro'] += doc['micro']
        state['claimed_micro'] = max(0, state.get('claimed_micro', 0) - doc['micro'])
        if (state.get('pending_claim') or {}).get('claim_id') == claim_id:
            state.pop('pending_claim', None)
            state.pop('claim_cooldown_until', None)
        state['events'].insert(0, {'text': f"Claim declined · ${doc['usd']:.4f} returned to your balance", 'kind': 'crew', 'at': now_iso()})

    await mutate_state(doc['session_id'], restore)
    await db.wallets.update_one({'address': doc['wallet_address'], 'next_claim_at': doc['next_claim_at']}, {'$unset': {'next_claim_at': ''}})
    return await get_claim(claim_id)


async def record_payout(claim_id, tx_hash, rcpt, transfers):
    """Validate an on-chain receipt against the claim; returns the updated claim or raises."""
    doc = await get_claim(claim_id)
    if (rcpt.get('to') or '').lower() != doc['token_address']:
        return await payout_failed(claim_id, 'That transaction does not call the game token contract.')
    paid = [(f, v) for f, t, v in transfers if t == doc['wallet_address']]
    if int(rcpt.get('status', '0x0'), 16) != 1:
        return await payout_failed(claim_id, 'The token transfer failed on-chain.')
    if sum(v for _, v in paid) < int(doc['amount']) * 98 // 100:
        return await payout_failed(claim_id, 'That transaction does not send the claimed amount to the player.')
    await db.claims.update_one({'claim_id': claim_id}, {'$set': {
        'status': 'sent', 'tx_hash': tx_hash, 'sent_at': utcnow(), 'paid_from': paid[0][0], 'block_number': int(rcpt['blockNumber'], 16), 'error': None}})

    def done(state):
        if (state.get('pending_claim') or {}).get('claim_id') == claim_id:
            state.pop('pending_claim', None)
        state['events'].insert(0, {'text': f"Claim paid · {doc['amount_tokens']:,.0f} {doc['symbol']} sent to your wallet", 'kind': 'sale', 'at': now_iso()})

    await mutate_state(doc['session_id'], done)
    return await get_claim(claim_id)


async def payout_failed(claim_id, error):
    await db.claims.update_one({'claim_id': claim_id}, {'$set': {'status': 'approved', 'error': error}, '$unset': {'tx_hash': ''}})
    doc = await get_claim(claim_id)
    await mutate_state(doc['session_id'], set_pending_status('approved'))
    raise HTTPException(400, error)
