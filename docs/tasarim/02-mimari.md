# 2. Mimari

## Bileşenler

```mermaid
flowchart LR
  B["Tarayıcı"] -->|"HTTP :3000"| FE["Frontend<br/>Next.js 16 · React · Zustand<br/>ui/"]
  B -->|"HTTP + WebSocket :8000<br/>(adres derlemede gömülü)"| BE["Backend<br/>FastAPI · Python 3.12<br/>nsg_ai/"]
  X["Harici xApp<br/>(müşterinin AI'ı)"] -->|"WS /ws/e2 · /ws/e2-real"| BE
  BE -->|"SQL"| DB[("PostgreSQL 16<br/>runs · campaigns · users …")]
  BE -->|"HTTPS (internet)"| LLM["OpenRouter<br/>claude-opus-5 / haiku-4.5"]
  BE -->|"HTTP :8090 toplu doğrulama<br/>TCP :8091 canlı köprü"| EN
  subgraph EN["Motor container'ı · nsg-engine:1.32-rxtrace"]
    ES["engine_service.py :8090"] --> RS["run_scenario.sh"]
    LB["live_bridge_server.py :8091"] --> RL["run_live_session.sh"]
    RS & RL --> RIC["FlexRIC nearRT-RIC"]
    RS & RL --> NS3["ns-3.42 mmWave RAN<br/>(E2 agent)"]
    RS & RL --> XA["xapp_kpm_rc<br/>(KPM okur, RC gönderir)"]
    NS3 <-->|"E2AP / SCTP"| RIC
    XA <-->|"E2AP"| RIC
  end
  subgraph INP["Backend süreci içinde"]
    TB["Tier B · tier_b.py + window_model.py"]
    RSIM["ric_sim.py (analitik RIC)"]
    REM["ns3_bridge (kapsama + canlı trafik)"]
  end
  BE --- INP
```

**Dağıtımda** (bkz. [09](09-kurulum-ve-isletim.md)) dördü de tek makinede Docker
Compose ile koşar: `db`, `engine`, `backend`, `frontend`. Motor portları
**dışarıya yayınlanmaz**; yalnız backend, compose ağı üzerinden `engine:8090` /
`engine:8091` adresine ulaşır. Nedeni: motor API'sinin kendi kimlik doğrulaması
yok ve bir zamanlar içinden kabuk komutu çalıştırılabildiği kanıtlandı
(`tools/security_probe.py`).

> **İnternet notu.** LLM çağrıları dışarıya (openrouter.ai) gider. İnternetsiz bir
> makinede AI sohbet ve AI ile senaryo üretimi çalışmaz. Diğer her şey (kanvas,
> editör, koşular, kampanyalar, RIC testi) internet istemez. Backend, geçerli
> olmasa bile bir `OPENROUTER_API_KEY` tanımlı değilse **hiç açılmaz**, bkz.
> [10](10-bilinen-sinirlar.md).

## Motor yolları: hangi ekran neyi çağırır

| Ekran / istemci | Uç nokta | Nereye gider | Nasıl |
|---|---|---|---|
| Sohbet | `POST /ai/modify` | OpenRouter | Konfigürasyon LLM'i, şemaya uygun çıktı üretir; `ai_interactions` tablosuna kaydedilir |
| Visual / Analytics — kapsama | `POST /run-simulation` | süreç içi `ns3_bridge.rsrp_heatmap` | Analitik RSRP ızgarası |
| Analytics — canlı trafik | `WS /ws/live-traffic` | süreç içi `ns3_bridge.estimate_live_throughput` | Tik başına tahmin |
| RIC Test (analitik) | `WS /ws/e2` | süreç içi `ric_sim` | Paylaşılan oturum; harici xApp aynı oturuma katılır |
| RIC Test (gerçek motor) | `WS /ws/e2-real` | motor `:8091` | `nsoran_live.py` proxy'si; kontrol satırları gerçek E2SM-RC'ye dönüşür. Aynı anda tek oturum. |
| Koşu (batch) | `POST /sim/run` | motor `:8091` | "Batch" burada **headless** demektir: canlı köprü üzerinden akar, kareler kaydedilir |
| Compare | `POST /ric/validate-nsoran` | motor `:8090` | Bloklayan tek istek, tek toplu sonuç |
| Campaign (slow) | `POST /campaigns` `tier=slow` | motor havuzu `:8091` | Her koşu bir motor örneğinde; havuz eşzamanlılığı sınırlar |
| Campaign (fast) | `POST /campaigns` `tier=fast` | süreç içi Tier B | Motor, kilit, köprü yok |

## Akış 1 — Gerçek motorda bir koşu (`/sim/run`)

```mermaid
sequenceDiagram
  participant UI as Arayüz
  participant BE as Backend (main.py)
  participant EP as engine_envelope / nsoran_params
  participant EN as Motor :8091
  participant DB as PostgreSQL
  UI->>BE: POST /sim/run {config}, frames, wall_cap_s
  BE->>EP: nsoran_params(config)
  Note over EP: Sayıları zarfa kıstırır, sahneyi ortalar,<br/>UE'leri binalardan/istasyonlardan iter,<br/>motorun parametre sözlüğünü üretir
  BE-->>UI: {run_id, status: running}
  BE->>EN: canlı oturum aç (parametreler)
  loop her KPM penceresi
    EN-->>BE: KPM satırları (xApp stdout)
    BE->>DB: koşu kendini yayınlar (kopan bağlantı veriyi götürmez)
  end
  BE->>BE: summarise_batch_frames
  BE->>DB: save_run (runs + run_frames, owner_id)
  UI->>BE: GET /sim/run/{id}  (yoklama)
  BE-->>UI: kareler + özet
```

Koşu statüleri iki yerden gelir. Motorun toplu sonucu: `ok`, `truncated` (ns-3
yolda öldü, eldeki KPM gerçek), `crashed`, `no_data`, `partial`, `busy`;
`ended_by` alanı nedenini söyler. Backend'in koşu kaydında ayrıca görülenler
(2026-09 veritabanında sayıldı): `done` / `completed` (canlı yoldan biten),
`wall-capped` (duvar saati bütçesi doldu; bu yolda olağan bitiş budur),
`timeout`, `interrupted`, `engine-closed`, `error`. Koşu boş dönerse **ilk bakılacak yer** motor
container'ındaki `/tmp/scn0.log` dosyasıdır; son satırı ns-3'ün tam komut
satırıdır (bkz. [06](06-motor-slow-tier.md#günlükler)).

## Akış 2 — Müşterinin xApp'i gerçek motoru sürüyor (`/ws/e2-real`)

```mermaid
sequenceDiagram
  participant X as Harici xApp
  participant BE as nsoran_live.py
  participant LB as live_bridge_server :8091
  participant BX as köprü xApp (FlexRIC)
  participant RAN as ns-3 RAN
  X->>BE: WS bağlan (token: header ya da ?token=)
  BE->>LB: oturum başlat
  loop
    RAN-->>BX: E2SM-KPM göstergesi (E2AP)
    BX-->>LB: stdout satırı
    LB-->>BE: satır
    BE-->>X: JSON KPM karesi
    X->>BE: kontrol JSON'u
    BE->>LB: komut satırı
    LB->>BX: FIFO
    BX->>RAN: E2SM-RC CONTROL (E2AP)
  end
```

Müşteri tarafında FlexRIC SDK gerekmez; indirilebilir bağlayıcı şablonu
`GET /ric/xapp-connector-real` ile alınır. Analitik karşılığı (`/ws/e2`)
aynı sözleşmeyi konuşur ama standart E2AP değildir, kendi JSON'umuzdur.

## Akış 3 — Fast tier'ın sayıları gerçek E2AP üzerinden (araçla)

Tier B'nin gerçek RIC ile konuşabildiği **kanıtlandı**, ancak bu yol **ürün
arayüzüne bağlı değildir**; araçlarla çalıştırılır:

`nsg_ai/e2_feed.py` bir dosyaya satır başına `<ölçüm adı> <imsi> <değer>` yazar →
`engines/flexric/nsg_feed.c` bu dosyayı FlexRIC'in emülatör ajanı içinden okur →
nearRT-RIC → üretimdeki `xapp_kpm_rc`. Çalıştırma: `tools/tier_b_serve.py`,
`tools/e2_spike.sh`. Ayrıntı: [07](07-fast-tier.md#gerçek-e2ap-üzerinden).

## Tasarım kararları

| Karar | Neden |
|---|---|
| Motor **ayrı bir container / ayrı dağıtım**, ayrı Ubuntu 20.04 | ns-3 mmWave çatalı en temiz 20.04'te derleniyor; ana sistemi bozmasın |
| Motor kodu **yalnızca yeniden kurulan imajla** ürüne ulaşır | Derleme ağacında derlemek ürünü değiştirmez; kayma sessizdir (bkz. [06](06-motor-slow-tier.md#motor-kodu-üç-yerde-yaşar)) |
| **Zarf (envelope) tek doğruluk kaynağı** | Aynı sabitler hem backend kıstırmasını hem LLM talimatını besler; genişletmek tek satır |
| Tier B **motorun parametrelerinden** kurulur, konfigürasyondan değil | İki katman aynı geometriyi görür; fark model hatası olarak okunabilir |
| **Kullanıcı başına izolasyon** `owner_id` ile | Başkasının kaydı 403 değil **404** döner (var olduğunu doğrulamamak için) |
| Yerel JWT, bulut kimliği değil | On-prem müşteriyle çelişmesin. `auth.decode_token` ileride OIDC/Keycloak geçişinin tek dikiş yeri |
| Motor portları yayınlanmaz | Kimlik doğrulamasız ve bir zamanlar kabuk komutu çalıştırılabilen bir API |
| Kampanya hükmü (`campaign.py`) motorsuz birim test edilir | Yanlış olmaması gereken kısım, motor koşusu gerektirmeden sınanabilsin |
