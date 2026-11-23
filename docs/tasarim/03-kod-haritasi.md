# 3. Kod haritası

## Üst seviye

```
nsg_ai_platform/
├── nsg_ai/            Backend (FastAPI). Üst seviye import'lar kullanır: BURADAN çalıştırılır.
├── ui/                Frontend (Next.js 16, React, Zustand, React Flow, Leaflet)
├── engines/           Motor yapıştırıcısı: script'ler, Dockerfile'lar, ns-3 yamaları
│   ├── nsoran/        ns-O-RAN motoru (gerçek E2) — 39 .patch + servis script'leri
│   └── flexric/       FlexRIC emülatör ajanı için Tier B besleme okuyucusu (nsg_feed.c)
├── tools/             Doğrulama, ölçüm, tarama araçları — indeks: tools/README.md
├── alembic/           Veritabanı göçleri (6 sürüm, baş: e5f6a7b8c9d0)
├── docker/            backend.Dockerfile, frontend.Dockerfile
├── docker-compose.yml Tek makinede tüm sistem (db, engine, backend, frontend)
├── experiments/       Harici xApp çakışma deneyi (platforma dokunmadan çalışır)
├── training_kit/      İndirilebilir RL eğitim kiti (Gymnasium ortamı) — şu an bozuk, bkz. 10
├── examples/          Örnek harici xApp
├── docs/              Dokümanlar (bu klasör: docs/tasarim/)
├── requirements.txt   Backend Python bağımlılıkları (sabitlenmiş sürümler)
└── .env.example       Ortam değişkeni şablonu
```

Ağır motor ağaçları (ns-3 derleme ağacı ≈6.8 GB, FlexRIC) **repoda değildir**.
Geliştirme için bir Docker imajı olarak taşınırlar, bkz. [08](08-gelistirme.md#motor-geliştirme).
Repo yalnızca **yapıştırıcıyı ve yamaları** tutar.

## Backend — `nsg_ai/`

| Dosya | Satır | Görevi |
|---|---|---|
| `main.py` | 3665 | FastAPI uygulaması: tüm uç noktalar, koşu işleri (`_sim_job`), `nsoran_params` (konfigürasyon → motor parametreleri), kampanya yürütücü, fast tier çağrısı. En büyük dosya; yeni iş genelde buraya değil, ilgili modüle yazılmalı. |
| `schema.py` | 2048 | `NSGSimulationConfig` Pydantic v2 şeması: hücreler, UE grupları, binalar, anomaliler, slice'lar, alan, ayarlar. `TIER_SCOPED`: hangi alanı hangi katmanın okuduğu ve nedeni. |
| `engine_envelope.py` | 757 | **Kural kitabı**: motorun doğrulanmış çalışma zarfı (hücre 1–16, UE 1–150, hücre başına 2–30 UE, 10–50 MHz, 600 MHz–60 GHz…). Aynı sabitler hem kıstırmayı hem LLM talimatını besler. |
| `llm_agent.py` | 583 | LangChain + OpenRouter ajanı: senaryo üretimi/düzenleme (config modeli `anthropic/claude-opus-5`, metin modeli `anthropic/claude-haiku-4.5`; `NSG_CONFIG_MODEL` / `NSG_TEXT_MODEL` ile değişir). İçe aktarılırken anahtar yoksa hata verir. |
| `ns3_bridge.py` | 950 | Analitik fizik: RSRP / SINR / yol kaybı, kapsama haritası (`rsrp_heatmap`), canlı trafik tahmini, `link_metrics`, `spectral_overlap`. Adındaki "ns3" tarihsel. |
| `tier_b.py` | 2043 | **Fast tier** akışı: senaryo → KPM kareleri (gerçek motorun köprüsüyle aynı biçimde). Mobilite, handover politikası, arızalar, enerji, uplink. *Modül başındaki "NE YOK" listesi eskidi*, bkz. [07](07-fast-tier.md). |
| `window_model.py` | 1475 | Fast tier'ın RAN'ı: bir KPM penceresi kapalı formda. Motorun kaynak kodundan okunan sabitler (AMC tabloları, TB boyutu, PRB formülü, anten elemanı, UPA kazancı, RU enerji akımları). |
| `mcs.py` | 144 | SINR → MCS → spektral verim, motorun kendi tablolarıyla. |
| `mi_error_model.py`, `mi_tables.py` | 198 / 732 | Motorun blok hata modelinin birebir portu. Taşındı, test edildi, **kapalı** (sezgisel yöntemin geçtiği bir doğrulamada başarısız). |
| `ric_sim.py` | 915 | Analitik RIC çekirdeği: değiştirilebilir RAN durumu, `get_kpm_report()` / `apply_control()` / `tick()`. `/ws/e2` ve RL kiti bunu kullanır. |
| `ric_session.py` | 294 | RIC test oturumlarını paylaşma: tarayıcı ve harici xApp aynı oturuma bağlanır. |
| `nsoran_live.py` | 1517 | Gerçek motor canlı proxy'si: WebSocket ↔ motorun `:8091` köprüsü; KPM satırlarını JSON karelere çevirir, kontrolü iletir. |
| `campaign.py` | 688 | Kampanya modeli: `CampaignSpec`, `expand()` (tekrarlar → tohumlar), `evaluate()` (özet + eşikler → PASS/FAIL). Saf ve motorsuz test edilebilir. |
| `engine_pool.py` | 226 | Motor örnekleri havuzu (`NSG_ENGINE_POOL`): eşzamanlılığı sınırlar, örnekleri dinlendirir. |
| `partition_advice.py` | 478 | Bir senaryoyu süreçlere bölmek neye mal olur — ölçümlere dayalı tavsiye. |
| `partition_run.py`, `partition_live.py`, `partition_pool.py` | 425 / 461 / 149 | Senaryoyu N ns-3 sürecine bölüp birleştirme (toplu ve canlı) ve örnekleri açıp kapatma. **Compose dağıtımında çalışmaz**, bkz. [10](10-bilinen-sinirlar.md). |
| `kpm_health.py` | 79 | Motorun kendi sayaçlarının bilinen hatalarını ürünün okuduğu yerde kontrol eder. |
| `e2_feed.py` | 201 | Tier B sayılarını FlexRIC C ajanının okuyacağı dosya biçimine yazar (araçlarla kullanılır, üründe değil). |
| `topology.py` | 332 | Prosedürel topoloji: altıgen, Manhattan ızgara, dağınık. |
| `store.py` | 542 | Kalıcılık: koşular, kareler, kampanyalar, senaryolar, kullanıcılar (SQLAlchemy). |
| `models.py` | 250 | ORM modelleri: `scenarios`, `runs`, `run_frames`, `campaigns`, `campaign_runs`, `users`, `ai_interactions`. |
| `db.py` | 85 | Bağlantı ve oturum; `NSG_DATABASE_URL` yoksa açılışta hata. |
| `auth.py` | 226 | Yerel kimlik: parola özeti, JWT, `current_principal` / `require_user` / `require_admin`, WebSocket kimliği. |
| `xapp_template.py` | 207 | İndirilebilir xApp bağlayıcı şablonu. |
| `test_*.py` | — | `test_auth`, `test_campaign`, `test_engine_pool`, `test_prompt_contract`, `test_ai_scenarios` (LLM ister) birim testleri; `test_isolation`, `test_ws_auth` çalışan bir backend'e karşı uçtan uca. |

## Frontend — `ui/`

| Yer | İçerik |
|---|---|
| `app/page.tsx` | Ana uygulama; `app/login`, `app/account`, `app/admin/users` sayfaları |
| `components/nsg-platform.tsx` | Kabuk: sohbet + önizleme + sekmeler |
| `components/model-preview.tsx` | Sekmeler: **visual**, **config**, **guardrails** (Sim Rules), **analytics**, **ric**, **compare**, **campaign** |
| `components/chat-interface.tsx`, `ConfigDiffViewer.tsx` | AI sohbeti ve önerilen değişikliğin farkı (kabul/ret) |
| `components/NetworkCanvas.jsx`, `nodes/*` | React Flow topoloji kanvası (CellNode, UENode, LiveUeNode, ObstacleNode) |
| `components/NetworkMap.tsx` | Leaflet + OpenStreetMap harita görünümü (tasarım katmanı; motorlar metre tabanlı kalır) |
| `components/ConfigEditor.jsx`, `SimulationRules.jsx` | Form editörü ve simülasyon kuralları |
| `components/CoverageMap.tsx`, `HeatmapOverlay.tsx` | RSRP kapsama haritası |
| `components/RicTestConsole.tsx`, `RicRealEngine.tsx`, `RicTopology.tsx`, `RicRealTopology.tsx`, `RicDataView.tsx`, `ManualRcControl.tsx` | RIC Test: analitik ve gerçek motor konsolları, canlı topoloji, veri görünümü, elle kontrol |
| `components/RicComparison.tsx` | Compare sekmesi (katmanlar / koşular arası) |
| `components/CampaignConsole.tsx`, `CampaignTrend.tsx`, `RunTimeline.tsx` | Kampanya kurma, eğilim, koşu zaman çizelgesi |
| `components/PartitionAdvice.tsx` | Bölme tavsiyesi |
| `components/history-sidebar.tsx`, `auth-gate.tsx` | Kayıtlı senaryolar; giriş kapısı |
| `store/useNSGStore.ts` | Tek Zustand store: konfigürasyon, koşular, WebSocket yaşam döngüleri |
| `lib/api.ts` | Tipli API istemcisi. Backend adresi `NEXT_PUBLIC_API_URL` ile **derleme anında** gömülür. |
| `lib/auth.ts`, `lib/users.ts`, `lib/metrics.ts`, `lib/partition.ts` | Token, kullanıcı yönetimi, metrik adları, bölme yardımcıları |
| `utils/flowMapper.ts` | Konfigürasyon ↔ kanvas dönüşümü |
| `utils/report.ts`, `campaignReport.ts`, `campaignExport.ts`, `exportData.ts` | PDF ve veri dışa aktarma |
| `utils/ricScore.ts`, `power.ts`, `ricPresets.ts`, `scenarioStore.ts`, `heatmapRender.ts` | Puanlama, watt biçimleme, hazır ayarlar, senaryo saklama, ısı haritası çizimi |
| `components/ui/*` | shadcn/ui bileşen kiti (elle yazılmış iş mantığı yok) |

## Motor yapıştırıcısı — `engines/nsoran/`

| Dosya | Görevi |
|---|---|
| `engine_service.py` | `:8090` — `GET /health`, `POST /run` → `run_scenario.sh`, KPM özetini JSON döner |
| `live_bridge_server.py` | `:8091` — canlı oturum; xApp stdout'unu akıtır, kontrol satırlarını FIFO'ya yazar |
| `run_scenario.sh`, `run_live_session.sh` | Orkestratör: RIC → ns-3 RAN → xApp; sınırlı ve kendini temizleyen |
| `Dockerfile`, `Dockerfile.base`, `build-slim.sh` | Yalın motor imajı; taban imajı ağ gerektiren tek parça, motor imajı ağsız derlenir |
| `*.patch` (39 adet) | ns-3 mmWave ağacına yapılan tüm değişiklikler; liste [06](06-motor-slow-tier.md#yamalar) |
| `demo_engine_up.sh` | Geliştirme makinesinde (WSL) motoru ayağa kaldırma |
| `fixtures/run_summary.golden.json` | Özet biçimi için altın örnek |

## Araçlar — `tools/`

60 dosya; her birinin hangi soruyu cevapladığı [`tools/README.md`](../../tools/README.md)
indeksinde. En sık kullanılanlar: `verify.py` (tek doğrulama komutu),
`test_fast_tier.py`, `tier_criterion.py` / `tier_breadth.py` / `tier_agreement.py`
(fast tier doğruluğu), `param_audit.py` (hangi alan motora ulaşıyor),
`fidelity_report.py`, `nsg_user.py` (kullanıcı yönetimi).
