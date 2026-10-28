import { useEffect, useState } from 'react';

const API = `${process.env.REACT_APP_BACKEND_URL}/api`;
const KEY = 'frostfish-visitor';
const BEAT_MS = 20000;

const visitorId = () => {
  let id = localStorage.getItem(KEY);
  if (!id) { id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`).replace(/[^A-Za-z0-9_-]/g, ''); localStorage.setItem(KEY, id); }
  return id;
};

export function usePresence() {
  const [online, setOnline] = useState(null);
  useEffect(() => {
    const visitor = visitorId();
    let timer, stopped = false;
    const beat = async () => {
      if (stopped || document.hidden) return;
      try { const res = await fetch(`${API}/presence`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ visitor }) }); if (res.ok) setOnline((await res.json()).online); }
      catch { /* keep last count */ }
      if (!stopped) timer = setTimeout(beat, BEAT_MS);
    };
    const visible = () => { clearTimeout(timer); if (!document.hidden) beat(); };
    document.addEventListener('visibilitychange', visible);
    beat();
    return () => { stopped = true; clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
  }, []);
  return online;
}
