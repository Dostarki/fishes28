# FISHESRH (Frostfish / fishesbetav1)

Gorev: https://github.com/Dostarki/fishesbetav1 reposunu cek ve calistir; ustune market genisletmesi.

## Mimari
- React (CRA) + react-three-fiber voxel ada; FastAPI + MongoDB; SIWE cuzdan girisi (Robinhood Chain 4663); token odemeli balikci satin alma.
- Gizli admin paneli: `/errorlogin404495959` — sifre backend/.env ADMIN_PASSWORD; .env de yoksa kodda sabit FISHESPRO4488 (admin.py DEFAULT_ADMIN_PASSWORD). Su an .env = FISHESPRO4488.
- Karakter tanimlari: `backend/game.py` CHARACTERS + `frontend/src/lib/gameData.js` CREW (ayna).

## Yapilanlar
### 2026-06 (ilk fork)
- Repo /app altina kuruldu, bagimliliklar yuklendi, backend+frontend calisiyor.
- Env: ROBINHOOD_CHAIN_ID=4663, public RPC, APP_URL=preview, ADMIN_PASSWORD, WalletConnect Project ID.
- Sol ust "FROSTFISH" yazisi -> "FISHESRH".

### 2026-06 (market genisletmesi) — test edildi (iteration_1, 25/25 backend, frontend %100)
- 4 yeni karakter: yacht (The Yachtsman, $100, 3x, 60s dinlenme), sub (The Submariner, $200, 5x, 30s, koyda dolasir),
  flagship (The Admiral, $400, 10x, 30s, dolasir), mothership (The Legend, $1000, 20x, KILITLI).
- Kazanc: carpanli karakterler Kaptan'in saatlik kazancinin (boat fiyati/48) katini kazanir; fiyattan bagimsiz. Worker'a satin alma aninda `hourly_usd` yazilir.
- Kilit: `locked_characters` ayari (varsayilan ['mothership']); quote ve confirm 423 doner. Admin panelde "7th character on sale" toggle.
- Admin panelde 7 fiyat alani; eksik fiyat gonderilirse mevcut deger korunur.
- Market: yatay kaydirmali serit + sag/sol oklar (market-prev/next-button), carpan rozeti, LOCKED/COMING SOON karti.
- 3D: yat/denizalti/amiral gemisi/mothership voxel modelleri; tum gemiler ortak global slot grid'inde (4.5 x 7 birim hucre) park eder -> park yerleri asla cakismaz. Denizalti ve amiral gemisi balik tutarken kendi hucresinde kucuk daire cizer.
- Kart gorselleri (CharacterArt) Kaptan kartindan turetilmis gelismis tekne SVG'leri.
- Testler: backend/tests/test_frostfish_characters.py eklendi; admin ayarlarini degistiren moduller dosya kilidiyle serilestirildi.

### 2026-06 (anlik market cap) — agent-tested (2 yeni backend testi gecti, screenshot)
- `chain.erc20_total_supply` + `pricing.total_supply/market_cap` (60s cache): mcap = totalSupply x canli token fiyati.
- `/api/config` -> `marketCapUsd`, `totalSupply`; `/api/admin/settings` -> `marketCap`.
- UI: `MarketCapBadge` (data-testid market-cap-badge/-value) ust header'da (karsilama) ve oyun ici HUD'da (hud-sub); launchpad'e link. Admin "Live status" karti: Market cap.


### 2026-06 (24 saatlik claim + admin onayli MetaMask odemesi) — test edildi (iteration_1: backend 29/29, admin+oyuncu UI gecti)
- Eski otomatik gonderim (TREASURY_PRIVATE_KEY) kaldirildi; claim artik "talep -> admin onayi -> MetaMask transferi" akisi.
- `POST /api/claim`: tum claimable bakiyeyi (min $0.05) talebe cevirir, cuzdan basina 24h cooldown (`db.wallets.next_claim_at`, oyun state'inde `claim_cooldown_until` + `pending_claim`). 409 (acik talep varken), 429 (24h dolmadan). `GET /api/claims` -> gecmis + `nextClaimAt`.
- Admin: `GET /api/admin/claims`; `POST /api/admin/claims/{id}/approve {payer}` (transfer talimati doner), `/reject {reason}` (bakiye iade + cooldown sifirlanir), `/confirm {txHash}` (zincirde ERC-20 Transfer logu dogrulanir, >=%98 -> `sent`).
- Backend modulleri: `backend/claims.py` (akis), `backend/state.py` (ortak optimistic mutate). Testler: `backend/tests/test_frostfish_claims.py` (11).
- Frontend: `ClaimPanel.jsx` (CLAIM / PENDING / NEXT CLAIM geri sayim + claim gecmisi) envanterde; `AdminClaims.jsx` admin panelin ilk karti: RainbowKit cuzdan baglama, odeyen cuzdan bakiyesi, "Approve & send" (MetaMask transfer -> confirm polling), "Decline", Open/All filtresi, 20s otomatik yenileme. Wagmi/RainbowKit provider'lari artik admin rotasini da sariyor (`index.js`).
- Demo oyuncu (bekleyen talep): /app/memory/demo_claimer.txt

### 2026-06 (online sayaci) — curl + screenshot ile dogrulandi
- `POST /api/presence {visitor}` (localStorage `frostfish-visitor` id, 20s heartbeat, sekme gizliyken durur) -> `db.presence` (TTL 300s); online = son 60s icinde sinyal gonderen herkes (giris sart degil). `GET /api/presence` sayiyi doner.
- `hooks/usePresence.js` + `OnlineBadge` (data-testid online-badge / online-count) karsilama ekrani ust barinda, market cap rozetinin yaninda.

## Backlog
- Gercek MetaMask ile uctan uca odeme (approve -> transfer -> sent) canli cuzdanla dogrulanmadi (otomasyonla mumkun degil); kullanici test etmeli.
- Reown panelinde preview/prod domainleri allowlist'e eklenmeli.
- Gemiler limana giderken ayni kanalda bulusabilir (sadece park yerleri garanti); istenirse liman yanasma seritleri genisletilebilir.
- Admin panel kilit toggle'i sadece mothership icin; baska karakter kilitlenecekse genel coklu toggle gerekir.
- Admin claim tablosu mobilde yatay kaydirmali (admin masaustu odakli).
