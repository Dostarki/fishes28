"""Hidden admin panel API: password login, economy settings, launchpad resolver, stats."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

import access
import chain
import claims as claim_flow
import pricing
from db import db
from game import CHARACTERS, hourly_rates

router = APIRouter(prefix='/api/admin')
ADMIN_TTL = timedelta(hours=12)
MAX_ATTEMPTS, LOCK_MINUTES = 5, 15
MAX_CSV_BYTES = 5 * 1024 * 1024
DEFAULT_ADMIN_PASSWORD = 'FISHESPRO4488'  # used when ADMIN_PASSWORD is not set in backend/.env
TX_RE = r'^0x[0-9a-fA-F]{64}$'


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def client_ip(request: Request):
    forwarded = request.headers.get('x-forwarded-for', '')
    return forwarded.split(',')[0].strip() if forwarded else (request.client.host if request.client else 'unknown')


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class SettingsRequest(BaseModel):
    tokenAddress: str
    treasuryAddress: str
    curveAddress: str | None = None
    launchpadUrl: str = Field(max_length=300)
    prices: dict[str, float]
    tokenUsdOverride: float | None = Field(default=None, ge=0)
    entryFeeEnabled: bool = False
    entryFeeUsd: float = Field(default=5.0, ge=0, le=100000)
    betaEnabled: bool = False
    nftGateEnabled: bool = False
    lockedCharacters: list[str] | None = None


class NftResolveRequest(BaseModel):
    url: str = Field(min_length=10, max_length=400)


class NftCheckRequest(BaseModel):
    address: str = Field(min_length=42, max_length=42)


class ResolveRequest(BaseModel):
    url: str = Field(min_length=10, max_length=300)


class ApproveRequest(BaseModel):
    payer: str = Field(min_length=42, max_length=42)


class RejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class PayoutRequest(BaseModel):
    txHash: str = Field(min_length=66, max_length=66, pattern=TX_RE)


async def require_admin(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Admin sign-in required.')
    session = await db.admin_sessions.find_one(
        {'token_hash': hashlib.sha256(authorization[7:].encode()).hexdigest(), 'expires_at': {'$gt': utcnow()}})
    if not session:
        raise HTTPException(401, 'Admin session expired. Sign in again.')
    return session


@router.post('/login')
async def login(body: LoginRequest, request: Request):
    ip = client_ip(request)
    attempt = await db.admin_attempts.find_one({'ip': ip})
    if attempt and attempt.get('locked_until') and attempt['locked_until'] > utcnow():
        raise HTTPException(429, f'Too many attempts. Try again in {LOCK_MINUTES} minutes.')
    expected = os.environ.get('ADMIN_PASSWORD') or DEFAULT_ADMIN_PASSWORD
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        count = (attempt or {}).get('count', 0) + 1
        update = {'$set': {'count': count, 'last_at': utcnow()}}
        if count >= MAX_ATTEMPTS:
            update['$set'].update(locked_until=utcnow() + timedelta(minutes=LOCK_MINUTES), count=0)
        await db.admin_attempts.update_one({'ip': ip}, update, upsert=True)
        raise HTTPException(401, 'Wrong password.')
    await db.admin_attempts.delete_one({'ip': ip})
    token = secrets.token_urlsafe(40)
    expires_at = utcnow() + ADMIN_TTL
    await db.admin_sessions.insert_one({'token_hash': hashlib.sha256(token.encode()).hexdigest(), 'expires_at': expires_at, 'ip': ip})
    return {'token': token, 'expiresAt': iso(expires_at)}


@router.post('/logout')
async def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.startswith('Bearer '):
        await db.admin_sessions.delete_one({'token_hash': hashlib.sha256(authorization[7:].encode()).hexdigest()})
    return {'ok': True}


async def settings_view():
    settings = await pricing.get_settings(db, fresh=True)
    meta = await pricing.token_meta(settings)
    view = {'tokenAddress': settings['token_address'], 'treasuryAddress': settings['treasury_address'],
            'curveAddress': settings.get('curve_address'), 'launchpadUrl': settings['launchpad_url'],
            'prices': settings['prices'], 'tokenUsdOverride': settings.get('token_usd_override'),
            'hourlyUsd': hourly_rates(settings['prices']), 'lockedCharacters': settings.get('locked_characters', []),
            'characters': [{'id': c['id'], 'name': c['name'], 'mult': c['mult'], 'rest': c['rest']} for c in CHARACTERS.values()],
            'symbol': meta['symbol'], 'decimals': meta['decimals'], 'updatedAt': settings.get('updated_at'),
            'entryFeeEnabled': bool(settings.get('entry_fee_enabled')), 'entryFeeUsd': settings.get('entry_fee_usd'),
            'betaEnabled': bool(settings.get('beta_enabled')), 'betaCount': await db.beta_wallets.count_documents({}),
            'nftGateEnabled': bool(settings.get('nft_gate_enabled')), 'nftGate': settings.get('nft_gate'),
            'claimEnabled': True, 'chainId': chain.CHAIN_ID}
    try:
        view['price'] = await pricing.token_price(settings)
    except Exception as e:
        view['price'] = None
        view['priceError'] = str(e)
    view['marketCap'] = await pricing.market_cap(settings)
    try:
        view['treasuryTokenBalance'] = (await chain.erc20_balance(settings['token_address'], settings['treasury_address'])) / 10 ** meta['decimals']
        view['treasuryEthBalance'] = (await chain.eth_balance(settings['treasury_address'])) / 1e18
    except Exception:
        view['treasuryTokenBalance'] = view['treasuryEthBalance'] = None
    return view


@router.get('/settings')
async def get_settings(_=Depends(require_admin)):
    return await settings_view()


@router.put('/settings')
async def put_settings(body: SettingsRequest, _=Depends(require_admin)):
    try:
        token = pricing.validate_address(body.tokenAddress, 'Token contract address')
        treasury = pricing.validate_address(body.treasuryAddress, 'Treasury address')
        curve = pricing.validate_address(body.curveAddress, 'Curve address') if body.curveAddress else None
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    current = await pricing.get_settings(db, fresh=True)
    prices = {}
    for cid in CHARACTERS:
        value = body.prices.get(cid, current['prices'].get(cid))
        if value is None or value <= 0:
            raise HTTPException(422, f'Price for {CHARACTERS[cid]["name"]} must be greater than 0.')
        prices[cid] = round(float(value), 2)
    locked = current.get('locked_characters', []) if body.lockedCharacters is None else body.lockedCharacters
    if any(cid not in CHARACTERS for cid in locked):
        raise HTTPException(422, 'Unknown character in the locked list.')
    if not body.launchpadUrl.startswith('http'):
        raise HTTPException(422, 'Launchpad link must start with http(s).')
    if body.entryFeeEnabled and body.entryFeeUsd <= 0:
        raise HTTPException(422, 'Entry fee must be greater than 0 USD when paid entry is on.')
    if body.nftGateEnabled and not current.get('nft_gate'):
        raise HTTPException(422, 'Read an OpenSea link first before turning the NFT gate on.')
    await pricing.save_settings(db, {'token_address': token, 'treasury_address': treasury, 'curve_address': curve,
                                     'launchpad_url': body.launchpadUrl.strip(), 'prices': prices, 'locked_characters': list(dict.fromkeys(locked)),
                                     'token_usd_override': body.tokenUsdOverride or None,
                                     'entry_fee_enabled': body.entryFeeEnabled, 'entry_fee_usd': round(float(body.entryFeeUsd), 2),
                                     'beta_enabled': body.betaEnabled, 'nft_gate_enabled': body.nftGateEnabled})
    return await settings_view()


@router.post('/nft/resolve')
async def nft_resolve(body: NftResolveRequest, _=Depends(require_admin)):
    try:
        gate = await access.resolve_opensea(body.url)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except Exception as e:
        raise HTTPException(400, f'Could not read that OpenSea page ({type(e).__name__}: {str(e)[:120]}). Check the link and try again.') from None
    await pricing.save_settings(db, {'nft_gate': gate})
    return {'nftGate': gate}


@router.delete('/nft')
async def nft_clear(_=Depends(require_admin)):
    await pricing.save_settings(db, {'nft_gate': None, 'nft_gate_enabled': False})
    return await settings_view()


@router.post('/nft/check')
async def nft_check(body: NftCheckRequest, _=Depends(require_admin)):
    settings = await pricing.get_settings(db, fresh=True)
    if not settings.get('nft_gate'):
        raise HTTPException(422, 'No NFT collection is configured yet.')
    try:
        address = pricing.validate_address(body.address, 'Wallet address')
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    try:
        holds = await access.holds_nft(settings['nft_gate'], address, fresh=True)
    except Exception as e:
        raise HTTPException(400, f'Ownership check failed: {str(e)[:160]}') from None
    return {'address': address, 'holds': holds, 'listed': await access.whitelisted(address), 'collection': settings['nft_gate']['name']}


@router.get('/beta')
async def beta_list(_=Depends(require_admin)):
    settings = await pricing.get_settings(db, fresh=True)
    return {'enabled': bool(settings.get('beta_enabled')), **(await access.whitelist_view())}


@router.post('/beta/import')
async def beta_import(file: UploadFile = File(...), _=Depends(require_admin)):
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, 'CSV is too large (max 5 MB).')
    addresses = access.parse_wallets(raw.decode('utf-8-sig', errors='ignore'))
    if not addresses:
        raise HTTPException(422, 'No wallet addresses (0x…) were found in that file.')
    total = await access.replace_whitelist(addresses, file.filename or 'csv')
    return {'imported': total, 'replaced': True, **(await access.whitelist_view())}


@router.delete('/beta')
async def beta_clear(_=Depends(require_admin)):
    await db.beta_wallets.delete_many({})
    return await access.whitelist_view()


@router.delete('/beta/{address}')
async def beta_remove(address: str, _=Depends(require_admin)):
    try:
        addr = pricing.validate_address(address, 'Wallet address')
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    result = await db.beta_wallets.delete_one({'address': addr.lower()})
    if not result.deleted_count:
        raise HTTPException(404, 'That wallet is not on the beta list.')
    return await access.whitelist_view()


@router.post('/resolve')
async def resolve(body: ResolveRequest, _=Depends(require_admin)):
    try:
        return await pricing.resolve_launchpad(body.url)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    except Exception:
        raise HTTPException(400, 'Could not read the Ponsfamily page. Check the link and try again.') from None


@router.get('/stats')
async def stats(_=Depends(require_admin)):
    purchases = await db.purchases.find({}, {'_id': 0}).sort('created_at', -1).limit(50).to_list(50)
    totals = await db.purchases.aggregate([{'$group': {'_id': None, 'usd': {'$sum': '$price_usd'}, 'count': {'$sum': 1}}}]).to_list(1)
    entries = await db.entry_payments.find({}, {'_id': 0}).sort('created_at', -1).limit(50).to_list(50)
    entry_totals = await db.entry_payments.aggregate([{'$group': {'_id': None, 'usd': {'$sum': '$fee_usd'}, 'count': {'$sum': 1}}}]).to_list(1)
    for e in entries:
        e['created_at'] = iso(e['created_at'])
    return {'wallets': await db.wallets.count_documents({}), 'purchaseCount': (totals[0]['count'] if totals else 0),
            'purchaseUsd': (totals[0]['usd'] if totals else 0), 'claimCount': await db.claims.count_documents({'status': 'sent'}),
            'pendingClaims': await db.claims.count_documents({'status': {'$in': list(claim_flow.OPEN)}}),
            'entryCount': (entry_totals[0]['count'] if entry_totals else 0), 'entryUsd': (entry_totals[0]['usd'] if entry_totals else 0),
            'purchases': purchases, 'entries': entries}


async def claims_payload():
    docs = await db.claims.find({}, {'_id': 0}).sort('created_at', -1).limit(200).to_list(200)
    sent = await db.claims.aggregate([{'$match': {'status': 'sent'}}, {'$group': {'_id': None, 'tokens': {'$sum': '$amount_tokens'}, 'usd': {'$sum': '$usd'}, 'count': {'$sum': 1}}}]).to_list(1)
    return {'claims': [claim_flow.claim_view(d) for d in docs],
            'open': sum(1 for d in docs if d['status'] in claim_flow.OPEN),
            'sentCount': sent[0]['count'] if sent else 0, 'sentTokens': sent[0]['tokens'] if sent else 0, 'sentUsd': sent[0]['usd'] if sent else 0}


@router.get('/claims')
async def admin_claims(_=Depends(require_admin)):
    return await claims_payload()


@router.post('/claims/{claim_id}/approve')
async def approve_claim(claim_id: str, body: ApproveRequest, _=Depends(require_admin)):
    try:
        payer = pricing.validate_address(body.payer, 'Payer wallet')
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    doc = await claim_flow.approve_claim(claim_id, payer)
    return {'claim': claim_flow.claim_view(doc), 'transfer': {
        'tokenAddress': pricing.to_checksum_address(doc['token_address']), 'to': pricing.to_checksum_address(doc['wallet_address']),
        'amount': doc['amount'], 'decimals': doc['decimals'], 'symbol': doc['symbol'], 'chainId': chain.CHAIN_ID}}


@router.post('/claims/{claim_id}/reject')
async def reject_claim(claim_id: str, body: RejectRequest, _=Depends(require_admin)):
    return {'claim': claim_flow.claim_view(await claim_flow.reject_claim(claim_id, body.reason))}


@router.post('/claims/{claim_id}/confirm')
async def confirm_claim(claim_id: str, body: PayoutRequest, _=Depends(require_admin)):
    tx_hash = body.txHash.lower()
    doc = await claim_flow.get_claim(claim_id)
    if doc['status'] == 'sent':
        return {'status': 'sent', 'claim': claim_flow.claim_view(doc)}
    if doc['status'] not in ('approved', 'sending'):
        raise HTTPException(409, f"Approve this claim first (it is {doc['status']}).")
    if await db.claims.find_one({'tx_hash': tx_hash, 'claim_id': {'$ne': claim_id}}, {'_id': 1}):
        raise HTTPException(409, 'This transaction was already used for another claim.')
    await db.claims.update_one({'claim_id': claim_id}, {'$set': {'status': 'sending', 'tx_hash': tx_hash}})
    try:
        rcpt = await chain.receipt(tx_hash)
    except Exception:
        raise HTTPException(503, 'Robinhood Chain RPC is unreachable. Please retry in a moment.') from None
    if not rcpt:
        return {'status': 'pending', 'claim': claim_flow.claim_view(await claim_flow.get_claim(claim_id))}
    transfers = list(chain.transfer_logs(rcpt, doc['token_address']))
    doc = await claim_flow.record_payout(claim_id, tx_hash, rcpt, transfers)
    return {'status': 'sent', 'claim': claim_flow.claim_view(doc)}


async def ensure_indexes():
    await db.admin_sessions.create_index('expires_at', expireAfterSeconds=0)
    await db.admin_attempts.create_index('ip', unique=True)
    await db.beta_wallets.create_index('address', unique=True)
