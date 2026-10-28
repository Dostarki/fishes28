# Frostfish claim flow: 24h cooldown per wallet, pending -> admin approve/reject -> MetaMask payout confirm
import os
import secrets
from datetime import datetime, timedelta

import pytest
import requests
from dotenv import dotenv_values
from eth_account import Account
from eth_account.messages import encode_defunct
from pymongo import MongoClient

ENV = dotenv_values('/app/backend/.env')
BASE_URL = (os.environ.get('REACT_APP_BACKEND_URL') or dotenv_values('/app/frontend/.env')['REACT_APP_BACKEND_URL']).rstrip('/')
API = f'{BASE_URL}/api'
ORIGIN = {'Origin': BASE_URL}
ADMIN_PW = os.environ.get('ADMIN_PASSWORD') or ENV['ADMIN_PASSWORD']
KEY = '0x' + secrets.token_hex(32)
ADDR = Account.from_key(KEY).address
PAYER = '0x000000000000000000000000000000000000dEaD'
T = 30

mongo = MongoClient(ENV['MONGO_URL'])[ENV['DB_NAME']]


def sign_in():
    ch = requests.post(f'{API}/auth/challenge', json={'address': ADDR, 'chainId': 4663}, headers=ORIGIN, timeout=T).json()
    sig = Account.sign_message(encode_defunct(text=ch['message']), private_key=KEY).signature.hex()
    body = {'address': ADDR, 'chainId': 4663, 'nonce': ch['nonce'], 'message': ch['message'], 'signature': sig if sig.startswith('0x') else '0x' + sig}
    r = requests.post(f'{API}/auth/verify', json=body, headers=ORIGIN, timeout=T)
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


@pytest.fixture(scope='module')
def player():
    headers = sign_in()
    yield headers
    mongo.claims.delete_many({'wallet_address': ADDR.lower()})
    mongo.games.delete_many({'wallet_address': ADDR.lower()})
    mongo.wallets.delete_many({'address': ADDR.lower()})


@pytest.fixture(scope='module')
def admin():
    r = requests.post(f'{API}/admin/login', json={'password': ADMIN_PW}, timeout=T)
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def fund(usd):
    res = mongo.games.update_one({'wallet_address': ADDR.lower()}, {'$set': {'balance_micro': int(usd * 1_000_000)}})
    assert res.matched_count == 1


def game(player):
    r = requests.get(f'{API}/game', headers=player, timeout=T)
    assert r.status_code == 200, r.text
    return r.json()


def claim(player):
    return requests.post(f'{API}/claim', headers=player, timeout=T)


def test_claim_needs_minimum_balance(player):
    r = claim(player)
    assert r.status_code == 400 and 'at least $0.05' in r.json()['detail']
    g = game(player)
    assert g['pending_claim'] is None and g['next_claim_at'] is None


def test_claim_creates_pending_request_and_starts_cooldown(player):
    fund(1.0)
    r = claim(player)
    if r.status_code == 503:
        pytest.skip('token price unavailable: ' + r.text)
    assert r.status_code == 200, r.text
    d = r.json()
    c = d['claim']
    assert c['status'] == 'pending' and abs(c['usd'] - 1.0) < 1e-6 and int(c['amount']) > 0 and c['amount_tokens'] > 0
    assert c['wallet_address'] == ADDR.lower() and 'session_id' not in c
    g = d['game']
    assert g['claimable_usd'] == 0 and abs(g['claimed_usd'] - 1.0) < 1e-6
    assert g['pending_claim']['claim_id'] == c['claim_id'] and g['pending_claim']['status'] == 'pending'
    next_at = datetime.fromisoformat(g['next_claim_at'].replace('Z', '+00:00'))
    assert timedelta(hours=23, minutes=55) < next_at - datetime.now(next_at.tzinfo) <= timedelta(hours=24)
    w = mongo.wallets.find_one({'address': ADDR.lower()})
    assert w['claim_count'] == 1 and w['next_claim_at'] > datetime.utcnow() + timedelta(hours=23)
    hist = requests.get(f'{API}/claims', headers=player, timeout=T).json()
    assert hist['claims'][0]['claim_id'] == c['claim_id'] and hist['nextClaimAt'] and hist['cooldownHours'] == 24


def test_second_claim_blocked_while_pending(player):
    fund(2.0)
    r = claim(player)
    assert r.status_code == 409 and 'waiting for approval' in r.json()['detail']


def test_admin_sees_pending_claim(player, admin):
    r = requests.get(f'{API}/admin/claims', headers=admin, timeout=T)
    assert r.status_code == 200, r.text
    d = r.json()
    mine = [c for c in d['claims'] if c['wallet_address'] == ADDR.lower()]
    assert len(mine) == 1 and mine[0]['status'] == 'pending' and d['open'] >= 1
    st = requests.get(f'{API}/admin/stats', headers=admin, timeout=T).json()
    assert st['pendingClaims'] >= 1 and 'claims' not in st


def test_admin_claims_require_auth():
    assert requests.get(f'{API}/admin/claims', timeout=T).status_code == 401
    assert requests.post(f'{API}/admin/claims/x/approve', json={'payer': PAYER}, timeout=T).status_code == 401


def test_admin_unknown_claim(admin):
    assert requests.post(f'{API}/admin/claims/nope/approve', json={'payer': PAYER}, headers=admin, timeout=T).status_code == 404
    assert requests.post(f'{API}/admin/claims/nope/reject', json={}, headers=admin, timeout=T).status_code == 404
    assert requests.post(f'{API}/admin/claims/nope/confirm', json={'txHash': '0x' + '1' * 64}, headers=admin, timeout=T).status_code == 404


def test_reject_returns_balance_and_resets_cooldown(player, admin):
    cid = game(player)['pending_claim']['claim_id']
    r = requests.post(f'{API}/admin/claims/{cid}/reject', json={'reason': 'test decline'}, headers=admin, timeout=T)
    assert r.status_code == 200, r.text
    assert r.json()['claim']['status'] == 'rejected' and r.json()['claim']['reject_reason'] == 'test decline'
    g = game(player)
    assert g['pending_claim'] is None and g['next_claim_at'] is None
    assert abs(g['claimable_usd'] - 3.0) < 1e-6 and g['claimed_usd'] < 1e-6  # $2 funded + $1 returned
    assert any('declined' in e['text'] for e in g['events'])
    assert 'next_claim_at' not in mongo.wallets.find_one({'address': ADDR.lower()})
    assert requests.post(f'{API}/admin/claims/{cid}/reject', json={}, headers=admin, timeout=T).status_code == 409


def test_approve_then_confirm_flow(player, admin):
    r = claim(player)
    assert r.status_code == 200, r.text
    cid = r.json()['claim']['claim_id']
    assert requests.post(f'{API}/admin/claims/{cid}/approve', json={'payer': 'bad'}, headers=admin, timeout=T).status_code == 422
    a = requests.post(f'{API}/admin/claims/{cid}/approve', json={'payer': PAYER}, headers=admin, timeout=T)
    assert a.status_code == 200, a.text
    d = a.json()
    assert d['claim']['status'] == 'approved' and d['claim']['payer'] == PAYER.lower()
    t = d['transfer']
    assert t['to'].lower() == ADDR.lower() and t['chainId'] == 4663 and t['amount'] == d['claim']['amount'] and t['tokenAddress'].lower() == d['claim']['token_address']
    assert game(player)['pending_claim']['status'] == 'approved'
    # approving twice is idempotent (MetaMask retry)
    assert requests.post(f'{API}/admin/claims/{cid}/approve', json={'payer': PAYER}, headers=admin, timeout=T).status_code == 200
    assert requests.post(f'{API}/admin/claims/{cid}/confirm', json={'txHash': 'bad'}, headers=admin, timeout=T).status_code == 422
    tx = '0x' + secrets.token_hex(32)
    c = requests.post(f'{API}/admin/claims/{cid}/confirm', json={'txHash': tx}, headers=admin, timeout=T)
    assert c.status_code in (200, 503), c.text
    if c.status_code == 200:
        assert c.json()['status'] == 'pending' and c.json()['claim']['status'] == 'sending' and c.json()['claim']['tx_hash'] == tx
        assert game(player)['pending_claim']['status'] == 'approved'
        # a claim with an in-flight tx can no longer be declined
        assert requests.post(f'{API}/admin/claims/{cid}/reject', json={}, headers=admin, timeout=T).status_code == 409
        mongo.claims.update_one({'claim_id': cid}, {'$set': {'status': 'approved'}, '$unset': {'tx_hash': ''}})
    # player is still blocked by the open claim
    fund(5.0)
    assert claim(player).status_code == 409
    rj = requests.post(f'{API}/admin/claims/{cid}/reject', json={}, headers=admin, timeout=T)
    assert rj.status_code == 200, rj.text


def test_confirm_requires_approved_claim(player, admin):
    r = claim(player)
    assert r.status_code == 200, r.text
    cid = r.json()['claim']['claim_id']
    c = requests.post(f'{API}/admin/claims/{cid}/confirm', json={'txHash': '0x' + secrets.token_hex(32)}, headers=admin, timeout=T)
    assert c.status_code == 409 and 'Approve this claim first' in c.json()['detail']
    assert requests.post(f'{API}/admin/claims/{cid}/reject', json={}, headers=admin, timeout=T).status_code == 200


def test_cooldown_blocks_new_claim_for_24h(player):
    mongo.wallets.update_one({'address': ADDR.lower()}, {'$set': {'next_claim_at': datetime.utcnow() + timedelta(hours=20)}})
    fund(1.0)
    r = claim(player)
    assert r.status_code == 429 and 'once every 24 hours' in r.json()['detail']
    hist = requests.get(f'{API}/claims', headers=player, timeout=T).json()
    assert hist['nextClaimAt'] is not None
    mongo.wallets.update_one({'address': ADDR.lower()}, {'$set': {'next_claim_at': datetime.utcnow() - timedelta(seconds=1)}})
    r = claim(player)
    assert r.status_code == 200, r.text
    assert game(player)['pending_claim']['status'] == 'pending'


def test_paid_claim_marks_sent_and_clears_pending(player, admin):
    # Simulate the on-chain confirmation outcome directly to verify state bookkeeping.
    cid = game(player)['pending_claim']['claim_id']
    requests.post(f'{API}/admin/claims/{cid}/approve', json={'payer': PAYER}, headers=admin, timeout=T)
    import asyncio
    import sys
    sys.path.insert(0, '/app/backend')
    doc = mongo.claims.find_one({'claim_id': cid})
    topic = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
    log = {'address': doc['token_address'], 'topics': [topic, '0x' + '0' * 24 + PAYER[2:].lower(), '0x' + '0' * 24 + ADDR[2:].lower()], 'data': hex(int(doc['amount']))}
    rcpt = {'status': '0x1', 'to': doc['token_address'], 'blockNumber': '0x10', 'logs': [log]}

    async def run():
        from motor.motor_asyncio import AsyncIOMotorClient
        import chain
        import claims as flow
        import state
        flow.db = state.db = AsyncIOMotorClient(ENV['MONGO_URL'])[ENV['DB_NAME']]
        return await flow.record_payout(cid, '0x' + 'ab' * 32, rcpt, list(chain.transfer_logs(rcpt, doc['token_address'])))

    updated = asyncio.run(run())
    assert updated['status'] == 'sent' and updated['paid_from'] == PAYER.lower() and updated['block_number'] == 16
    g = game(player)
    assert g['pending_claim'] is None and g['next_claim_at'] is not None  # cooldown keeps running after payout
    assert any('Claim paid' in e['text'] for e in g['events'])
    r = requests.post(f'{API}/admin/claims/{cid}/confirm', json={'txHash': '0x' + 'ab' * 32}, headers=admin, timeout=T)
    assert r.status_code == 200 and r.json()['status'] == 'sent'
    assert claim(player).status_code == 429
