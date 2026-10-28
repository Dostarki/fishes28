"""Optimistic-concurrency game state updates shared by the player API and the admin API."""
from fastapi import HTTPException

from db import db
from game import VERSION, advance, initial_state

CARRY = ('claim_cooldown_until', 'pending_claim')


async def mutate_state(sid, apply=None):
    for _ in range(8):
        state = await db.games.find_one({'session_id': sid}, {'_id': 0})
        if not state:
            raise HTTPException(401, 'Your island could not be found. Please sign in again.')
        revision = state['revision']
        if state.get('version') != VERSION:
            keep = {k: state[k] for k in ('name', 'username_set') if state.get('username_set')}
            if state.get('entry_paid'):
                keep['entry_paid'] = True
            keep.update({k: state[k] for k in CARRY if state.get(k)})
            state = {**initial_state(sid), **keep, 'session_id': sid, 'wallet_address': state['wallet_address']}
        advance(state)
        if apply:
            apply(state)
        state['revision'] = revision + 1
        result = await db.games.replace_one({'session_id': sid, 'revision': revision}, state.copy())
        if result.modified_count:
            return state
    raise HTTPException(409, 'The island is busy. Please try again.')
