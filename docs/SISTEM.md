# NSG AI Platform — Sistem Dokümanı

> Yapay zekâ destekli 5G/RAN **senaryo tasarımı + simülasyon + RIC testi** platformu.
> Kullanıcı senaryoyu tarif eder → LLM konfigürasyonu üretir → simülasyon motorları
> çalışır → sonuçlar ve RIC testleri arayüzde gösterilir.
>
> *Son güncelleme: 2026-09-18, `nsg-engine:1.25`. İngilizce giriş noktası için
> kök dizindeki [`README.md`](../README.md).*

---

## 1. Üç katman

```
┌──────────────────────────────────────────────────────────────────┐
│  FRONTEND  (ui/ — Next.js + React, Zustand store)                │
│  Sohbet · Visual · Analytics · RIC Test · Campaign · Compare     │
│  Geçmiş kenar çubuğu · Giriş ekranı · /admin/users               │
└───────────────┬──────────────────────────────────────────────────┘
                │  HTTP / WebSocket
┌───────────────▼──────────────────────────────────────────────────┐
│  BACKEND  (nsg_ai/ — FastAPI, Python)                            │
│  • LLM ajanı  • config şeması + doğrulama  • REST  • WS canlı akış│
│  • yerel JWT auth  • PostgreSQL kalıcılık  • kampanya yürütücü    │
└───────────────┬──────────────────────────────────────────────────┘
                │  motorun İKİ portuna HTTP
┌───────────────▼──────────────────────────────────────────────────┐
│  ENGINES  (repo dışı derlenmiş ağaçlar — bkz. engines/README.md) │
│  Analytical core (repo içi) · Coverage grid (repo içi) · ns-O-RAN │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. Motorlar

| Motor | Hız | Gerçekçilik | Ne zaman |
|---|---|---|---|
| **Analytical core** (`nsg_ai/ric_sim.py`) | anlık | kendi tik-başı modelimiz | RIC Test canlı döngüsü, xApp geliştirme |
| **ns-O-RAN** (container) | **duvar saatinin ~%4'ü** | en yüksek — gerçek E2AP/SCTP | nihai doğrulama, müşterinin sorgulayacağı her şey |
| **Coverage grid** (`ns3_bridge.rsrp_heatmap`) | saniyeler | analitik yol kaybı | Analytics'teki RSRP ısı haritası |

**Önemli fark:** Analytical core sayıları *hesaplar*; ns-O-RAN gerçek bir 5G
yığınından (FlexRIC RIC + ns-3.42 mmWave RAN, gerçek E2 protokolü) *ölçer*.

### Hız gerçeği — bilerek yavaş

ns-O-RAN her TTI'da gerçek kanal, PHY, MAC, scheduler ve HARQ işletiyor. Hız
kaldıraçları **ölçülerek tüketildi** (2026-09-15/16):

- en iyi tek konfig kaldıracı **1.31×** (shadowing kapalı — ve ağı iyimser yapıyor)
- dört kaldıraç birlikte **1.29×** — üst üste binmiyorlar
- tek yapısal kaldıraç FR1 numerology (µ1) **1.35×**, ürüne alınmadı
- **real time ~26× uzakta**

Yani motoru hızlandırmayı planlamayın; **paralel koşmayı** planlayın
(`tools/engine_pool_up.sh`, örnekler arası ~3.8× ölçüldü).

### Ufuk: bir batch koşusu birkaç simüle saniye görür

KPM toplama penceresi ufkun **kendisidir** ve takas doğrusaldır:
`settings.kpm_collect_s` duvar saatini simüle zamana çevirir. Tipik: 120 s
pencere → ~4.5 simüle saniye (3 hücre / 6 UE). Bu tasarımın sonucu, hata değil —
bir anomaliyi `start_s=5`'te tetiklerseniz hiç ateşlenmez.

---

## 3. Gerçek E2 koşusu: basınca ne oluyor

```
Tarayıcı (Compare sekmesi)
   │  POST /ric/validate-nsoran
   ▼
Backend  (platform distro, :8000)
   │  POST http://127.0.0.1:8090/run
   ▼
ns-O-RAN container  (Ubuntu-2004-nsoran distro, nsg-engine:1.25)
   │  run_scenario.sh:  RIC ▸ ns-3 RAN (E2 agent) ▸ KPM xApp
   │  gerçek E2AP/SCTP el sıkışması + E2SM-KPM göstergeleri
   ▼
{status, ended_by, energy.span_s,
 measurements:{DRB.UEThpDl, RRU.PrbUsedDl, L3servingSINR, …}}
```

**İki motor yolu vardır, karıştırmayın:**

| Port | Ne | Kim çağırır |
|---|---|---|
| `:8090` | batch doğrulama — bloklayan, tek toplu sonuç | **yalnız Compare** sekmesi |
| `:8091` | canlı köprü — kare kare akış | Campaign ve RIC Test |

Bu kod tabanında "batch" **headless** demektir; `run_batch_simulation` aslında
canlı yoldur.

### Koşu statüleri

`ok` · `truncated` (ns-3 yolda öldü, eldeki KPM gerçek) · `crashed` ·
`no_data` · `partial` · `busy`. `ended_by` nedenini söyler. Bir koşu kendini
koşarken `/run/snapshot`'a yayınlar, böylece kopan bağlantı saatleri götürmez.

---

## 4. Servisler ve portlar

| Servis | Nerede | Port |
|---|---|---|
| Frontend (Next.js) | platform distro | 3000 |
| Backend (FastAPI) | platform distro | 8000 |
| Motor — batch doğrulama | `Ubuntu-2004-nsoran`, container | 8090 |
| Motor — canlı köprü | `Ubuntu-2004-nsoran`, container | 8091 |
| PostgreSQL | platform distro | 5433 |

Motor portları **bilerek yalnız loopback**'e bağlı: motor API'si çağıranına
güvenecek şekilde tasarlandı, dolayısıyla kutunun dışından erişilebilir olmamalı.

---

## 5. Kalıcılık ve kimlik

- **PostgreSQL :5433** — `runs`, `campaigns`, `scenarios`, `users`,
  `ai_interactions`. Alembic ile göç; `preflight.py` göç başını da kontrol eder.
- **Yerel JWT auth** — bulut değil, on-prem müşteriyle çelişmesin diye. Sahiplik
  `owner_id` üzerinden; kullanıcı adı yalnızca etikettir, yeniden adlandırma
  veriyi koparmaz. `auth.decode_token` ileride Keycloak/OIDC'ye geçişin tek
  dikişidir.
- **Kullanıcı yönetimi** — arayüzde `/admin/users` (oluştur, yeniden adlandır,
  şifre, rol, devre dışı, sil), kutuda `tools/nsg_user.py`. İkincisi backend
  kapalıyken de çalışır — tek yönetici kilitlenirse geri dönüş yolu odur.

---

## 6. Ne nerede duruyor

| Parça | Konum |
|---|---|
| Frontend | `ui/` |
| Backend, şema, LLM ajanı | `nsg_ai/` |
| ns-O-RAN glue (script'ler, Dockerfile, yamalar) | `engines/nsoran/` |
| Motor haritası | `engines/README.md` |
| Araç indeksi | `tools/README.md` |
| ns-O-RAN derlenmiş motor | ayrı WSL distro `Ubuntu-2004-nsoran` → `nsg-engine:1.25` |
| Veri envanteri / config olgunluk raporu | `docs/engine-veri-ve-config-durumu.md` |

> **Tera5G/VIAVI yığını tamamen kaldırıldı.** RSRP kapsama haritası artık dış bir
> binary çağırmıyor, `ns3_bridge.rsrp_heatmap` içeride hesaplıyor (2026-09-15).
> Eski analytics yığını emekli — bkz. `engines/README.md`.

---

## 7. Motor değişikliği ürüne nasıl ulaşır

**Yalnızca yeniden kurulmuş image üzerinden.** Host ağacında derlemek ürünün
koştuğu hiçbir şeyi değiştirmez. Motor kodu üç yerde yaşar (repo → host
`/root/nsoran` → container) ve kayma sessizdir:

```bash
# motor distrosunda
bash engines/nsoran/build-slim.sh nsg-engine:<yeni-tag>

# platform distrosunda — üçünün aynı olduğunu kanıtlar
python3 tools/engine_sync.py
```

`verify.py` kaymada başarısız olur.

---

## 8. Geliştirme — hızlı başlangıç

```bash
# Backend (nsg_ai/ dizininden — üst-seviye 'import ns3_bridge' bunu gerektirir)
cd nsg_ai && ../venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000

# Frontend
cd ui && npm run dev            # http://localhost:3000

# Motor (yalnızca gerçek E2 testi gerektiğinde)
wsl -d Ubuntu-2004-nsoran
  dockerd &                     # otomatik başlamaz
  docker start nsg-engine
```

Bir şey ölü görünüyorsa **önce şunu koşun** — her bağlantıyı ihtiyaç duyulduğu
yerden kanıtlar ve her kırık için düzeltmeyi yazar:

```bash
python3 tools/preflight.py          # --fix ile onarabildiğini onarır
```

**Tuzak:** WSL boştaki distroyu askıya alır; bu, ayrılmış backend/frontend'i
öldürür ve arayüz "Failed to fetch" der (prompt değil). `nsg_demo_up.bat` tam da
bu yüzden bloklayan bir keepalive ile biter — o pencereyi kapatmayın.

---

## 9. Değişikliği doğrulama

```bash
python3 tools/verify.py --fast      # birim + kontrat, geometri, şema, UI tipleri
python3 tools/verify.py             # + preflight ve motor/repo senkronu
python3 tools/verify.py --all       # + motor kabul testi (yavaş, container ister)
```

Her koşuda **neyi kontrol etmediğini** de yazar. Araç dizini: `tools/README.md`.
