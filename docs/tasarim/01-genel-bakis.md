# 1. Genel bakış

## Platform ne yapar

NSG AI Platform, 5G radyo erişim ağı (RAN) için bir **senaryo tasarım, simülasyon
ve RIC test** ortamıdır:

1. **Tarif et.** Kullanıcı sohbet penceresine senaryoyu yazar ("stadyumda 3 hücre,
   yoğun eMBB trafiği"). Bir LLM bunu doğrulanmış bir konfigürasyona
   (`NSGSimulationConfig`) çevirir.
2. **Düzenle.** Konfigürasyon görsel bir kanvasta (hücreler, UE grupları, binalar),
   bir form editöründe ya da harita üzerinde düzenlenir.
3. **Koştur.** Senaryo iki katmandan birinde koşar: ns-3 tabanlı **gerçek motor**
   (yavaş, en yüksek doğruluk) ya da kapalı-form **hızlı katman**.
4. **İncele.** KPM sayaçları (throughput, PRB kullanımı, blok hatası, handover,
   enerji…) Analytics, Compare ve Campaign ekranlarında zaman serisi, tablo ve
   rapor olarak gösterilir.
5. **RIC testi.** Kullanıcı **kendi xApp'ini** (yapay zekâsını) WebSocket
   üzerinden bağlar. xApp KPM okur, kontrol komutu gönderir (hücre kapat, güç
   değiştir, slice kotası, handover) ve etkisini bir sonraki raporda görür.

Hedef kitle: xApp / RAN algoritması geliştiren ekipler ve bunları test etmek
isteyen operatör veya araştırma grupları. Konumlanma notu: ticari rakipler
(VIAVI, Keysight, Spirent) çok daha olgundur. Bu platformun farkı, AI ile senaryo
tasarımı ve açık kaynak, gerçek E2AP konuşan bir motor üzerinde çalışmasıdır.

## Simülasyon katmanları

| Katman | Kodda adı | Nasıl çalışır | Hız | Ne zaman |
|---|---|---|---|---|
| **Slow tier** | Tier A, "engine", ns-O-RAN | ns-3.42 mmWave RAN + FlexRIC near-RT RIC, gerçek E2AP/SCTP. Her TTI simüle edilir (µ=2'de 4000 adım/sn). | Gerçek zamandan **yüzlerce kat yavaş** | Nihai doğrulama; birinin sorgulayacağı her sayı |
| **Fast tier** | Tier B | Bir KPM penceresini (≈1 sn) **tek adımda** kapalı formda hesaplar. Sabitler motorun kaynak kodundan okunmuştur. | Gerçek zamandan **onlarca kat hızlı** | Uzun ufuk (yüzlerce saniye), geniş taramalar, kampanyalar |
| **Analitik RIC çekirdeği** | `ric_sim` | Tik başına basit fizik + durum; kendi JSON "E2" sözleşmesi | Anlık | RIC Test sekmesindeki canlı döngü, xApp geliştirme |
| **Kapsama haritası** | `rsrp_heatmap` | Analitik yol kaybıyla RSRP ızgarası | Saniyeler | Analytics'teki ısı haritası |

Hızın kaynağı kanal hesabı değil, **zaman adımıdır**. Ayrıntı:
[07-fast-tier.md](07-fast-tier.md). Motor hızlandırma çalışmaları ölçülerek
kapatıldı. Motoru hızlandırmayı değil, **paralel koşturmayı** planlayın:
[06-motor-slow-tier.md](06-motor-slow-tier.md#hız).

## Ana yetenekler

- AI ile senaryo üretimi ve düzenlemesi (OpenRouter üzerinden LLM)
- Prosedürel topoloji üretimi: altıgen, Manhattan ızgara, dağınık
- OpenStreetMap tabanlı harita görünümü ve bina içe aktarma
- Gerçek motorda toplu (batch) koşu, canlı koşu, kampanya (çok senaryo × çok
  tohum × eşik) ve iki kampanyanın karşılaştırılması
- Harici xApp bağlantısı: analitik çekirdekte (`/ws/e2`) ve gerçek motorda
  (`/ws/e2-real`). Müşterinin FlexRIC SDK'sına ihtiyacı yoktur, düz JSON yeter.
- Motor tarafında: slicing (QCI başına MAC kotası), RU uyku durumları, hücre
  kapatma, anten azimut/eğim, mobilite (rota + A3/A2), arıza enjeksiyonu,
  binalar, uplink
- Yerel JWT kimlik doğrulama, kullanıcı başına veri izolasyonu, admin ekranı
- PDF / veri dışa aktarma, RL eğitim kiti (bozuk, bkz. [10](10-bilinen-sinirlar.md))

## Kavram sözlüğü

| Terim | Anlamı |
|---|---|
| **gNB / hücre (cell)** | 5G baz istasyonu. Konfigürasyonda `base_stations[]`. Bir sahada birden çok sektör olabilir. |
| **UE** | Kullanıcı cihazı. Konfigürasyonda `ue_groups[]` (sayı, konum, mobilite, servisler). |
| **RIC (near-RT)** | RAN Intelligent Controller. Burada EURECOM **FlexRIC**'in `nearRT-RIC` binary'si. |
| **xApp** | RIC üzerinde çalışan kontrol uygulaması; test edilen "yapay zekâ". |
| **E2 / E2AP** | RAN ile RIC arasındaki O-RAN arayüzü ve protokolü (ASN.1 üzerinden SCTP). |
| **E2SM-KPM** | Ölçüm servis modeli: RAN'ın periyodik sayaç raporları (`DRB.*`, `RRU.*`, `TB.*`…). |
| **E2SM-RC** | Kontrol servis modeli: xApp'in RAN'a gönderdiği komutlar. |
| **KPM penceresi / frame** | Bir ölçüm raporu aralığı (≈1 sn simüle zaman). Arayüzdeki bir "frame" bir KPM raporudur. |
| **Horizon (ufuk)** | Bir koşunun kapsadığı **simüle** süre. Gerçek motorda duvar saatiyle doğrusal takas. |
| **TTI / numerology (µ)** | Zaman dilimi ve alt taşıyıcı aralığı. Motor her zaman µ=2 (0.25 ms slot) koşar, bkz. [10](10-bilinen-sinirlar.md). |
| **Slice / 5QI / QCI** | Ağ dilimi ve servis kalitesi sınıfı; slice kotası PRB payını sınırlar. |
| **A3 / A2** | 3GPP handover ölçüm olayları (komşu daha iyi / servis eden zayıf). |
| **REM** | Radio Environment Map — kapsama ısı haritası. |
| **Envelope (zarf)** | Motorun doğrulanmış çalışma aralığı: hücre, UE, bant genişliği sınırları. `nsg_ai/engine_envelope.py`. |
| **TIER_SCOPED** | Yalnızca bir katmanın okuduğu konfigürasyon alanları ve nedenleri. `schema.TIER_SCOPED`. Editörde "ANALYTICAL" rozeti olarak görünür. |
| **Partition** | Büyük bir senaryoyu birden çok ns-3 sürecine bölmek (hız ↔ doğruluk takası). |
