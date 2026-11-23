# 4. API

Backend bir FastAPI uygulamasıdır; **canlı ve eksiksiz şema** çalışan backend'in
`/docs` (Swagger) ve `/openapi.json` adreslerindedir. Konfigürasyon şeması ayrıca
`GET /schema` ile alınır. Aşağıdaki tablo kaynak koddan (`nsg_ai/main.py`)
üretildi; açıklamalar ilgili fonksiyonun docstring'inin ilk paragrafıdır.

## Kimlik doğrulama

- Giriş: `POST /auth/login {username, password}` → `{access_token, token_type, user}`.
- HTTP isteklerinde `Authorization: Bearer <token>`. Token ömrü
  `NSG_AUTH_TOKEN_TTL_HOURS` (varsayılan 12 saat).
- WebSocket'te token `Authorization` başlığıyla (tarayıcı dışı istemciler için
  önerilen) ya da `?token=` ile (tarayıcı başka yol bırakmaz) gelir. Zorunluluk
  açıkken kimliksiz soket, okunabilir bir nedenle **1008** koduyla kapatılır.
- ¹ **kullanıcı** koruması `NSG_AUTH_REQUIRED=1` iken giriş ister, `0` iken anonimi
  geçirir (iş "kimseye" yazılır). Dağıtım paketi `1` ile gelir.
- Başkasına ait bir kayıt **404** döner, 403 değil: API bir kimliğin var
  olduğunu başka kullanıcılara doğrulamaz.

> ⚠️ `POST /ric/validate-nsoran` zorunluluk açıkken bile **anonim çağrıyı kabul
> eder** (`current_principal` kullanıyor, `require_user` değil). Giriş yapmamış biri
> gerçek motoru çalıştırabilir. Paket backend'i yalnızca `127.0.0.1`'e açtığı için
> risk sınırlı, ama düzeltilmesi gerekir: bkz. [10](10-bilinen-sinirlar.md).

## Uç noktalar

### Kimlik ve kullanıcı yönetimi

| Yöntem | Yol | Koruma | Açıklama (kaynak koddaki docstring) |
|---|---|---|---|
| `POST` | `/auth/register` | ilk kullanıcıda açık, sonra admin | Create a login. |
| `POST` | `/auth/login` | açık | Exchange username + password for a bearer token. |
| `GET` | `/auth/me` | açık (anonim geçerli cevap) | Who the caller is. Anonymous is a valid answer, not a 401 — the frontend uses it to decide whether to show a login form, and it reports whet… |
| `POST` | `/auth/password` | kullanıcı¹ | Change your OWN password, proving it with the current one. |
| `GET` | `/auth/users` | admin |  |
| `POST` | `/auth/users/{user_id}/disable` | admin |  |
| `POST` | `/auth/users/{user_id}/role` | admin |  |
| `POST` | `/auth/users/{user_id}/password` | admin | An administrator sets someone's password — the only reset path, since there is no email to send a link to on an on-prem appliance. |
| `POST` | `/auth/users/{user_id}/username` | admin | Rename a login. |
| `DELETE` | `/auth/users/{user_id}` | admin |  |
| `POST` | `/auth/users/{user_id}/enable` | admin |  |

### AI (LLM)

| Yöntem | Yol | Koruma | Açıklama (kaynak koddaki docstring) |
|---|---|---|---|
| `POST` | `/ai/modify` | kullanıcı¹ | Apply a natural-language modification to the current simulation configuration. |
| `POST` | `/ai/interactions/{interaction_id}/outcome` | kullanıcı¹ | Say explicitly what happened to a generated config. |
| `GET` | `/ai/interactions` | kullanıcı¹ | The generator's own record: what it was asked, what it built, what was kept. |

### Koşular

| Yöntem | Yol | Koruma | Açıklama (kaynak koddaki docstring) |
|---|---|---|---|
| `POST` | `/sim/run` | kullanıcı¹ | Start a batch simulation run of the given scenario on the real engine. Returns immediately with a run id; poll GET /sim/run/{id} for status … |
| `GET` | `/sim/runs` | kullanıcı¹ | List runs (metadata only, newest first) — no frames, so it stays small. |
| `GET` | `/sim/run/{run_id}` | kullanıcı¹ | Full dataset for a finished run (frames + summary), or the live record while it's still running. 404 rather than 403 when it belongs to some… |

### Kampanyalar

| Yöntem | Yol | Koruma | Açıklama (kaynak koddaki docstring) |
|---|---|---|---|
| `POST` | `/campaigns` | kullanıcı¹ | Start a campaign: every item run at every seed, each scored against the thresholds. Returns immediately; poll GET /campaigns/{id}. |
| `GET` | `/campaigns` | kullanıcı¹ | Campaigns, newest first, without per-run detail. |
| `GET` | `/campaigns/history` | kullanıcı¹ | A suite's campaigns lined up in time: scenario × campaign, with the metrics each was judged on. |
| `GET` | `/campaigns/{campaign_id}` | kullanıcı¹ | Full campaign record: thresholds, per-run verdicts and violations. |
| `GET` | `/campaigns/{campaign_id}/datasets` | kullanıcı¹ | Every run's frames for this campaign, in one response. |
| `POST` | `/campaigns/{campaign_id}/stop` | kullanıcı¹ | Ask a running campaign to stop after the current run. |
| `GET` | `/campaigns/{campaign_id}/compare` | kullanıcı¹ | Diff this campaign against a baseline: per scenario, what moved and by how much, and whether the move is bigger than the baseline's own seed… |
| `DELETE` | `/campaigns/{campaign_id}` | kullanıcı¹ | Forget a campaign. The underlying run datasets are left alone — they are evidence, and other campaigns or comparisons may reference them. |

### Senaryolar ve topoloji

| Yöntem | Yol | Koruma | Açıklama (kaynak koddaki docstring) |
|---|---|---|---|
| `GET` | `/scenarios` | kullanıcı¹ |  |
| `POST` | `/scenarios` | kullanıcı¹ | Store a named snapshot of a configuration. |
| `DELETE` | `/scenarios/{scenario_id}` | kullanıcı¹ |  |
| `POST` | `/scenario/generate` | kullanıcı¹ | Procedural topology — hex, Manhattan street grid, or scattered. |

### RIC ve motor doğrulama

| Yöntem | Yol | Koruma | Açıklama (kaynak koddaki docstring) |
|---|---|---|---|
| `POST` | `/ric/validate-nsoran` | **anonim de kabul** | Run the REAL ns-O-RAN engine — FlexRIC (real near-RT RIC) + ns-3.42 mmWave RAN over real E2AP/SCTP, running as a container worker — DRIVEN B… |
| `GET` | `/ric/xapp-connector` | kullanıcı¹ | Return a turnkey Python xApp connector, pre-filled with the WebSocket URL and the caller's session id. The user downloads this, drops their … |
| `GET` | `/ric/xapp-connector-real` | kullanıcı¹ | Turnkey Python xApp connector for the REAL engine (/ws/e2-real). The user drops their trained model into decide() and runs it; their control… |
| `GET` | `/ric/training-kit` | kullanıcı¹ | Return the self-contained RL training kit as a zip: the Gymnasium environment + its simulation engine (physics, sim, schema) + a training ex… |

### WebSocket

| Yöntem | Yol | Koruma | Açıklama (kaynak koddaki docstring) |
|---|---|---|---|
| `WEBSOCKET` | `/ws/live-traffic` | kullanıcı¹ (soket içinde) | Live Traffic Mode: streams per-slice throughput estimates. |
| `WEBSOCKET` | `/ws/e2` | kullanıcı¹ (soket içinde) | Simplified E2 interface for RIC testing, with SHARED SESSIONS so a browser and a user's external xApp (their AI under test) can drive/observ… |
| `WEBSOCKET` | `/ws/e2-real` | kullanıcı¹ (soket içinde) | LIVE external-xApp session on the REAL ns-O-RAN engine (not the analytical RicSimulation behind /ws/e2). Proxies the WebSocket to the engine… |

### Yardımcı ve durum

| Yöntem | Yol | Koruma | Açıklama (kaynak koddaki docstring) |
|---|---|---|---|
| `POST` | `/run-simulation` | kullanıcı¹ | Translate an NSGSimulationConfig to NS-3 JSON and run ns3-dev-rem-gen. |
| `POST` | `/partition/advice` | açık | How should this scenario be split across processes, and what does it cost? |
| `POST` | `/config/normalize` | açık | Run a config through the schema and return it COMPLETE. |
| `GET` | `/schema` | açık | Return the JSON schema for NSGSimulationConfig. Useful for: - Auto-generating frontend forms (react-hook-form + zod) - Validating configs cl… |
| `GET` | `/live-metrics` | kullanıcı¹ | Pull real-time KPIs from InfluxDB (populated by the analytics engine). Returns empty series when no simulation is running. |
| `POST` | `/generate-report` | kullanıcı¹ | LLM executive summary of the latest simulation KPIs. |
| `GET` | `/envelope` | açık | The measured engine envelope, so the CANVAS can warn while a scenario is being drawn instead of the backend clamping it silently at run time… |
| `GET` | `/engine-pool` | kullanıcı¹ | How many ns-O-RAN engines will actually serve a campaign. |
| `GET` | `/engine-status` | kullanıcı¹ | Analytics engine health check. |
| `POST` | `/stop` | kullanıcı¹ | Stop the currently running simulation. |
| `POST` | `/reset` | kullanıcı¹ | Reinitialise the LLM agent (clears any in-memory state). |
| `GET` | `/health` | açık | Liveness probe for container orchestration. |

> Eski analitik yığından kalan üç uç nokta, `GET /live-metrics`, `GET /engine-status`
> ve `POST /stop`, artık çalışmayan bir servise (`localhost:29501`) sorar ve
> boş / `offline` döner. `POST /run-simulation`'ın docstring'i hâlâ harici bir binary
> anlatıyor, ama kapsama haritası artık süreç içinde `ns3_bridge.rsrp_heatmap` ile
> hesaplanıyor.

## WebSocket protokolleri

### `/ws/live-traffic` — canlı trafik tahmini
1. İstemci bağlanır ve **tek bir mesaj** gönderir: tam `NSGSimulationConfig`.
2. Sunucu her `settings.report_interval_ms`'de bir kare yollar:
   `{"time", "eMBB", "uRLLC", "mMTC", "voice"}` (Mbps).
3. Durdurmak için bağlantı kapatılır; ayrı bir "stop" komutu yoktur.

### `/ws/e2` — analitik RIC testi (paylaşılan oturum)
- **Oluştur:** ilk mesaj konfigürasyondur (çıplak ya da `{"config": …, "label": …}`).
  Cevap: `{"type": "session", "session_id": "<8 hex>"}`. Tarayıcı bu kimliği gösterir.
- **Katıl:** `{"join": "<session_id>", "label": "my-xapp"}`. Harici xApp tarayıcının
  oturumuna böyle katılır.
- Sonra herkese her rapor aralığında
  `{"type": "kpm", "tick", "cells", "ue_groups", "slices", "control_state"}` yayınlanır.
- İstemci istediği an kontrol gönderir: `handover`, `cell_power`, `slice_quota`,
  `reset`. Komut herkese `{"type": "control_event", …}` olarak yansır, gönderene
  ayrıca `{"type": "control_ack", …}` gider.
- Standart E2AP **değildir**; aynı kapalı döngü anlamına sahip kendi JSON
  sözleşmemizdir. Bağlayıcı şablonu: `GET /ric/xapp-connector`.

### `/ws/e2-real` — gerçek motorda canlı xApp
- `/ws/e2` ile aynı erişilebilir JSON sözleşmesi, ama her komut gerçek E2SM-RC'ye
  dönüşür ve gerçek ns-3 RAN'a uygulanır. Müşterinin FlexRIC SDK'sına ihtiyacı yoktur.
- Motor tek senaryo koşturduğu için **aynı anda tek oturum** vardır.
- Bağlayıcı şablonu: `GET /ric/xapp-connector-real`. Ayrıntılı akış:
  [02](02-mimari.md#akış-2--müşterinin-xappi-gerçek-motoru-sürüyor-wse2-real).
