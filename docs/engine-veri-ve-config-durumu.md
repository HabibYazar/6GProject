# ns-O-RAN Engine: Veri Envanteri ve Config Olgunluk Raporu

> ⚠️ **Bu, 2026-07-29 tarihli bir ÖLÇÜM RAPORUDUR, güncel durum tablosu değil.**
> Gövdesindeki sayılar o gün ölçüldü ve kanıt olarak duruyor; aradan geçen sürede
> bazıları değişti. Güncel sistem tablosu için [`SISTEM.md`](SISTEM.md), güncel
> ölçüm için ilgili aracı koşun (`tools/param_audit.py`, `tools/fidelity_report.py`).
>
> **O günden bu yana değişenler — okumadan önce bilin:**
>
> | Raporun dediği | Bugün |
> |---|---|
> | param audit 153 wired / 12 tier-scoped / 0 inert | **157 wired / 0 inert**; ham backlog 478 → **64 gerçek**, gerisi gerekçesiyle reddedildi |
> | Tera5G karşılaştırmaları (BÖLÜM 2) | Tera5G yığını **tamamen kaldırıldı** (2026-09-15); karşılaştırma tarihsel değer taşıyor, kurulum tarifi değil |
> | "sim ~%0.5 gerçek-zaman" | ölçüldü: **~%4** (batch, ürün yolu); hız kaldıraçları tüketildi, real time ~26× uzakta |
> | anomali ufku notları | `settings.kpm_collect_s` (1.13+) ufku duvar saatiyle doğrusal olarak uzatıyor |
>
> Ayrıca o tarihte bilinmeyen iki şey: `DRB.PdcpSduDelayDl` aynı seed'de **14×**
> oynuyor (gürültü, karar vermeyin), ve flex-TTI scheduler assert'i belirli
> seed'lerde hâlâ çöküyor.

*Tarih: 2026-07-29 · O günkü durum: fidelity harness **9/9** "ALL LEVERS WIRED" · param audit **153 wired / 12 tier-scoped / 0 INERT** — gece nöbetçisi ikisini de koşuyor (01:00, `reports/`)*

> **Güncel notlar (2026-07-29):** BÖLÜM 2 baştan ölçülerek yazıldı. Eski
> sürümdeki ❌ satırlarının çoğu artık geçersiz: `param_audit.py` şemadaki 165
> parametre alanının **hiçbirinin** sessizce düştüğünü göstermiyor. Geriye kalan
> 12-15 alan "analitik katman" kapsamında ve bu artık config editöründe
> **ANALYTICAL** rozetiyle kullanıcıya da söyleniyor (`schema.TIER_SCOPED`).
>
> **2026-07-18 notları (hâlâ geçerli):** Live Traffic sekmesi motor-beslemeli
> (analitik üretici o paneli beslemiyor; kapsama/REM için yaşıyor). Canlı oturum
> UE'leri harita ve React Flow'da gerçek pozisyonlarıyla hareket ediyor.
> UL gecikme ölçülüyor (2.1-2.6 ms). Ölçülmüş kesin sınır: hareket-kaynaklı
> handover churn hiçbir pratik ufukta gözlemlenemez (sim ~%0.5 gerçek-zaman;
> 5 dk canlı = 2.6 sim-s) — HO sayacı kontrol-kaynaklı mobilite için anlamlı.

Bu belge iki soruyu cevaplar:
1. **Motor hangi verileri üretiyor, hangilerini üretebilir, hangileri elimizin dışında?** (her metrik ne anlama geliyor, bizim için neden değerli)
2. **Config'imiz Tera5G/VIAVI sınıfı bir config'e göre ne düzeyde ve motor onu ne kadar işliyor?**

---

## BÖLÜM 1 — Veri Envanteri

### Okuma anahtarı (yeni başlayan için)

Bir 5G baz istasyonunun "sağlık raporu" üç soruya iner:
- **Ne kadar veri taşıdım?** → throughput (Mbps), volume (bit)
- **Bunu ne kadar kaynak/kalite ile yaptım?** → PRB (radyo kaynağı birimi), SINR (sinyal/girişim kalitesi, dB), BLER (bozuk blok oranı), MCS (modülasyon = kanal ne kadar iyiyse o kadar "hızlı alfabe")
- **Bedeli neydi?** → enerji (Joule/Watt), gecikme (ms)

RIC test platformu için değer zinciri şu: **AI bir karar verir → bu sayılar oynar → oynadığını ölçebiliyorsak test gerçektir.** Bir metrik AI'ın kararına tepki vermiyorsa süs; veriyorsa kanıttır. Aşağıdaki "değeri" sütunları hep bu gözle yazıldı.

### 1A. ÜRETİYORUZ — gerçek, ölçülmüş, kanıtlı (E2SM-KPM üzerinden)

| Metrik | Nedir | Bizim için değeri |
|---|---|---|
| `DRB.UEThpDl` (UE başına, kbps) | Kullanıcının gerçekte aldığı DL hız | **Manşet metrik.** Compare leaderboard'u bununla sıralanır; slice kotası kanıtı bununla verildi (−%68) |
| `DRB.PdcpSduBitRateUl` / `VolumeUl` | UE'nin yukarı yönde taşıdığı hız/hacim | **Yeni.** "Motor DL-only" etiketini sildi; talep 2× → UL 2.2× kanıtlı |
| `L3servingSINR` (dB) | Servis hücresinin sinyal/girişim kalitesi | AI'ın ana "gözü": hücre uyut → 7.65→0.7 dB, bina koy → +17 dB. Kol kanıtlarının çoğu bunun üstünden |
| `RRU.PrbUsedDl` (UE başına) | Kullanılan radyo kaynak bloğu | Kaynak/verimlilik göstergesi; slice kotasının doğrudan hedefi. (Kesirli ortalamanın 0'a kırpılma bug'ı düzeltildi, `prb-truncation-fix.patch`) |
| BLER % (TB sayaçlarından türetilmiş) | Bozuk blok oranı | Link sağlığı; agresif enerji tasarrufunun gizli maliyetini gösterir |
| `CARR.PDSCHMCSDist.Bin1-6` | MCS dağılımı (QPSK/16QAM/64QAM) | Kanal kalitesinin ikinci görünümü; SINR ile tutarlılık kontrolü |
| PDCP DL seti (`Volume/BitRate/DelayDl`) | Taşınan hacim, hız ve **ortalama gecikme** | Gecikme ölçülüyor ama henüz hiçbir skor okumuyor → uRLLC senaryoları için hazır bekleyen fırsat |
| `L1M.RS-SINR.Bin*` | SINR histogram kovaları | Dağılım görünümü (ortalama yalan söyleyebilir, histogram söylemez) |
| **Enerji: hücre başına Joule** | Gerçek radyo enerji modeli ölçümü | **Ana demo hikâyesi:** 2 hücre uyut → −%45.4 J, uyuyan hücre 0 J, yükü devralan hücrenin tüketimi artıyor. Config-hesabı değil, ölçüm (şimdilik batch yolu) |
| Canlı per-UE satırlar (IMSI, hücre, SINR, thp, PRB) | Canlı oturumda UE tablosu | Müşterinin xApp'inin göreceği ham beslenti |
| RC kontrol döngüsü (istek/ack sayısı) | E2SM-RC kapalı döngü kanıtı | "Kontrol gerçekten RAN'a gitti" ispatı |

**Gerçek kontrol yüzeyi** (veri değil ama veriyi oynatan eller): hücre aç/kapa + güç (Energy_state), handover (Style 3), **slice kotası (Style 2)**, fault injection.

### 1B. ÜRETEBİLİRİZ — motor destekliyor, kablolanmadı (rafta hazır)

| Aday | Neden mümkün | Tahmini iş |
|---|---|---|
| ~~UL gecikme~~ **YAPILDI (2026-07-18)** | `DRB.PdcpSduDelayUl` CU-UP raporunda; ölçülen 2.1-2.6 ms | — |
| Gecikme bazlı skor (uRLLC) | `PdcpSduDelayDl` zaten ölçülüyor, sadece skora bağlanacak | Küçük |
| Hücre başına bağlı UE sayısı (`RRC.ConnMean` benzeri) | `ueMap.size()` DU raporunda zaten dolaşıyor | Küçük |
| CQI raporu | Scheduler'da `m_wbCqiRxed` mevcut | Küçük-orta |
| Canlı enerji | Joule satırları ns-3 logunda; köprü tail edecek | Orta |
| ~~HO sayacı / `hoSinrDifference` kolu~~ **YAPILDI** | NGAP id churn'den `handovers`; `ho_sinr_margin_db` + tüm A3/A2/TTT katmanı config'te | — |
| ~~`speed_mps`'in gerçekten uygulanması~~ **YAPILDI (2026-07-17)** | Per-UE hız motora iniyor; 17.9× beklenen = 17.9× ölçülen | — |
| UL PRB kullanımı | Kodda `ulPrbUsage = 0 // future`; UL sembolleri MAC'te sayılabilir | Orta |

### 1C. ÜRETEMİYORUZ — endüstri standardı ama motorun paradigması/mimarisinin dışında

Tera5G/VIAVI'nin O1 listesiyle karşılaştırınca dürüst eksik listesi:

| Eksik | Endüstride ne işe yarıyor | Neden bizde yok |
|---|---|---|
| **RSRP/RSRQ (UE başına)** | Kapsama ölçümü, HO kararlarının standardı | mmWave modülü SINR raporluyor; RSRQ girişim ölçüm çerçevesi ister. *Kısmi telafi: analitik katman RSRP üretiyor* |
| **MIMO/beam KPM** (`MaxLayerDlMimo`, `AverageLayersDl`, BeamId) | Massive-MIMO verimlilik analizi | Motor analog 8×8 beamforming — 3GPP "MIMO katmanı" kavramı bu modelde yok |
| **Carrier Aggregation / çoklu-bant KPM** | Gerçek şebekeler çok bantlı | Motor tek global taşıyıcı (bilinen "yapma" kararı) |
| **RRC/kontrol düzlemi istatistikleri** (ConnEstabAtt/Succ, paging) | Erişilebilirlik (accessibility) KPI'ları | Sim tüm UE'leri t=0'da bağlar; gerçekçi RRC churn'ü yok |
| ~~**QoS-flow başına çoklu servis**~~ **ÇÖZÜLDÜ** | Bir UE'de aynı anda ses+video+veri akışı | Artık her servis kendi akışı olarak kuruluyor (`test_multiservice.py`: ikinci servis UE'nin taşıdığı hızı ölçülebilir biçimde yükseltiyor) |
| **O1/VES telemetri arayüzü** (PEE.* formatları dahil) | SMO/OSS entegrasyonu | Bizim yüzeyimiz E2 (+kendi WS'imiz); O1 ayrı büyük iş |
| **Paket seviyesi gecikme yüzdelikleri** (p99 vb.) | uRLLC SLA kanıtı | Ortalama PDCP gecikmesi var; dağılım için iz (trace) altyapısı gerekir |

> **Konumlandırma cümlesi:** VIAVI bu genişliği yıllardır ekiplerle inşa ediyor. Bizim iddiamız genişlik değil: **"kendi xApp'ini gerçek E2 üzerinden bağla, kararlarının throughput/SINR/PRB/enerji/slice etkisini ölçülmüş veriyle gör"** — bu çekirdek dilim bizde çalışıyor ve 7 kolu otomatik kanıtlı. Eksik listesi utanılacak şey değil; ne satmadığımızı bilmek, ne sattığımızı netleştirir.

---

## BÖLÜM 2 — Config Olgunluğu: Tera5G'ye Karşı Biz

*Bu bölüm 2026-07-29'da **ölçülerek** yeniden yazıldı. Kaynaklar: Tera5G tarafı
`Tera5G/api/RICTest_tests_API.yaml` + gerçek bir koşunun `metadata.json`'ı
(`data/rsg_data/2-simulation`); bizim taraf `NSGSimulationConfig.ui_json_schema()`
+ `tools/param_audit.py --cached`.*

### Kaba ölçek

| | Tera5G (VIAVI RIC Test) | Biz |
|---|---|---|
| Config yaprak parametresi | **284** | **165** |
| — kapsam-dışı saydığımız kısım | 105 (E2 conformance 80, Security 16, O1 9) | — |
| — kıyaslanabilir kısım | ~179 | 165 |
| Motora gerçekten ulaşan | ölçülmüyor (araç yok) | **153 wired · 12 tier-scoped · 0 inert** |

Tera5G bölüm bazında: Cells 60, E2 Test 80, Scenario_Generation 23, UE_Groups 22,
Anomalies 17, System 16, Security 16, Data Set Generator 13, Slices 11,
App Emulation 11, O1 9.

### Bizim config: motor gerçekte ne işliyor?

**Sınıflandırma:** ✅ motor payload'ına iniyor (audit ölçümü) + fidelity kanıtı ·
🟡 iniyor ama pratik sınırı var · 🔵 tier-scoped: analitik katmanda gerçek,
ns-O-RAN'a gitmiyor (UI'da ANALYTICAL rozeti) · ❌ sessizce düşüyor

| Config alanı | ns-O-RAN motoru | Not |
|---|---|---|
| Hücre sayısı, konumlar (x,y,**z**) | ✅ L2 | Node yükseklikleri de gerçek (2026-07-26) |
| Frekans + bant genişliği | 🟡 L1 | **Global tek taşıyıcı** — ilk hücrenin bandı herkese uygulanır; hücre başına bant yok |
| `numerology` | ✅ | Geçiyor, ama motor NrNumerology2/3 tanımlı — dışı belgelenmiş biçimde 2-3'e kırpılıyor |
| `tx_power_dbm` (hücre başına) | ✅ L3 | Uyku = güç+noise-figure |
| `radio.sleep` (RU uyku durumu) | ✅ 2a | micro/light/deep + shutdown timer + uyanma gecikmesi. Ölçüm: 3875→3372→3290 W; throughput değişmedi |
| `settings.ru_*_current_a` | ✅ | Uyku derinliklerinin indiği taban akım modeli |
| Binalar (sınır/duvar/kat/oda) | ✅ L4 | Gerçek LOS/NLOS; +17 dB kanıtı. `floors`/`rooms_x/y` artık geçiyor |
| UE sayısı + spawn + `height_m` | ✅ | 60 m drone 9.59 vs 1.5 m'de 17.77 Mbps/UE ile kanıtlı |
| Slice (`qos_id` + hedef hız) | ✅ L5+L6 | Bearer QCI'sı gerçek; DL + UL trafiği; kota canlı E2 ile |
| **UE başına çoklu servis** | ✅ | Her servis kendi akışı (`test_multiservice.py`) |
| **Çağrı dinamiği** (`avg_call_duration_s`, `avg_idle_duration_s`, `call_distribution`, `protocol`) | ✅ | Fidelity harness'ta ayrı kol: "L5 call dynamics" |
| Anten `azimuth_deg` / `tilt_deg` | ✅ 2c | Element `3gpp` yapılınca gerçek — eski "❌ sektör kavramı yok" hükmü **yanlıştı** |
| Anten `type`, `gain_dbi`, beamwidth'ler, mMIMO dijital yönlendirme | 🔵 | Element tipi `settings.antenna_element`'ten; kazanç dizi geometrisinden türüyor; 65° 3GPP element deseninde derlenmiş |
| `mobility.speed_mps` + `waypoints` + `waypoint_mode` | ✅ 2b | Per-UE hız (17.9× beklenen = 17.9× ölçülen); waypoint yolu hücre sınırı geçtiği için HO üretebilen tek mobilite kolu |
| `mobility.pattern` | 🔵 | Motor hız+waypoint ile yürüyor; pattern analitik katmanda aktivite faktörü |
| HO katmanı (`ho_sinr_margin_db`, `handover_mode`, `ho_ttt_ms`, `ho_a3_entry`, `ho_hysteresis_db`, `ho_a2_threshold_db`, DynamicTtt eğrisi, `q_rx_lev_min_dbm`, `outage_threshold_db`) | ✅ 2b | Upstream'de RRC handover değerlendirme döngüsü ölüydü (t=0'da bir kez karar); düzeltildikten sonra kol gerçek. Sınır: hareket-kaynaklı churn batch penceresinde görünmez |
| `shadowing_enabled` | ✅ | Bağlandığında ölçüldü: **+%156** throughput. Önceki tüm kol sayıları gölgesiz kanalda ölçülmüştü |
| `scenario` (UMa/UMi/RMa/UMa-Buildings) | ✅ | UMi→RMa = **+%98.8** |
| `fast_fading_enabled` | 🔵 | 3GPP spektrum modeli zaten hep fade ediyor; bayrak analitik katmanı kapatıyor |
| PHY/MAC/RLC katmanı (`error_model`, `amc_*`, `harq_*`, `rlc_*`, `cqi_*`, gürültü figürleri, `ra_*`, `sym_per_slot`, `fixed_mcs_*`, `blockage_*`, `o2i_*`, X2/S1-U bütçesi …) | ✅ | ~100 alan; audit hepsinin payload'ı değiştirdiğini ölçüyor. **Tera5G'de bu yüzey hiç açık değil** |
| `report_interval_ms` | ✅ | Artık geçiyor (eski "0.5 s'e sabit" notu geçersiz) |
| `duration_s` | 🟡 | Geçiyor ama koşuyu duvar-saati kapağı bitirir; hedef, garanti değil |
| `random_seed` | ✅ | Aynı seed mobilite iz hash'ini ve throughput'u 2 hane tekrarlıyor |
| `area.*` + `resolution_m` | 🔵 | REM kapsama haritasını çerçeveler; gerçek motor grid örneklemiyor |
| `slice_type` | 🔵 | Motoru `qos_id`+hız sürüyor; `slice_type` canlı KPM'i slice bazında etiketliyor (başka yolda gerçek) |
| `anomalies[]` | ✅ | 4 tip, koşu ortasında gerçekten enjekte ediliyor: `radio_degradation` (güç düşüşü), `cell_outage`, **`noise_rise`** (gürültü figürü yükselir — güç sabit kalır, sadece kalite düşer; ölçüm: +40 dB → DL −%21.4, SINR 16.56→10.20 dB), **`handover_block`** (anchor HO kabulünü reddeder). Sınır: `handover_block`'un *etkisi* batch penceresinde gösterilemez (statik UE, HO isteği yok) — kabul edildiği ve koşuyu bozmadığı doğrulanmış durumda |

**Cevap — "motor config'i tamamıyla işleyebiliyor mu?": Sessizce düşen alan
kalmadı.** 165 parametrenin 153'ü motor payload'ını değiştiriyor, 12'si
belgelenmiş biçimde analitik katman kapsamında ve bu artık kullanıcıya da
söyleniyor. Ölçüm ≠ fizik garantisi: audit "payload değişti mi"ye bakar,
`fidelity_report.py` ise 9 kolun sonucu gerçekten oynattığını ölçer; ikisi
birlikte koşuyor.

### Tera5G'ye göre ÖNEMLİ eksiklerimiz (2026-07-29, önem sırasıyla)

1. **Test tezgâhı katmanı** (`System`): `batch_mode`, `start_date` sanal saati,
   `cluster_instances` ile paralel koşu, `data_set_format` CSV/JSON dışa aktarım,
   `cell_score_formula`, `rsrp_cutoff`, `ns3_version` v2/v3 seçimi. Bizde koşu
   tek atımlık, çıktı tarayıcıda. **Şu an en büyük ürün farkı bu.**
2. ~~**Bant başına HO/reselection eşik katmanı**~~ → **YAPISAL SINIR, kapanmaz**
   (ölçüldü 2026-07-29). `scenario-zero`'da `nLteEnbNodes = 1`: tek bir LTE
   anchor var, mmWave hücreleri onun secondary cell'leri, ve `OutageThreshold` /
   `HoSinrDifference` / `SecondaryCellHandoverMode` **o tek `LteEnbRrc`'nin**
   attribute'ları. Eşik hücreye değil karar vericiye ait, karar verici bir tane —
   hücre başına alan açmak motorun yok sayacağı bir alan açmak olurdu. Tera5G'de
   mümkün çünkü orada her hücre kendi RRC'sini taşıyor. Tek RRC üzerindeki
   son iki knob **bağlandı (2026-07-29)**: `ho_joining_timeout_ms` (hedef taraf,
   "too late" penceresi) ve `ho_leaving_timeout_ms` (kaynak taraf, "too early"),
   ikisi de 5 ms ve 2 s uçlarında ns-3 tarafından kabul edilmiş durumda
   (`test_ho_timers.py`). Üçüncüsü `AdmitRrcConnectionRequest` bilinçli dışarıda:
   ölçülmüş biçimde ns-3'ü başlangıçta çökertiyor (bkz. `test_anomalies.py`).
3. ~~**Anomali çeşitliliği**~~ → **KISMEN KAPANDI (2026-07-29)**: `noise_rise`
   (kalite arızası — Tera5G'nin "rsrq/rssinr offset"inin gerçek karşılığı) ve
   `handover_block` (kontrol döngüsünün *başarısız olduğu* senaryo) eklendi;
   şema tip-dışı büyüklüğü (`noise_rise_db` bir `cell_outage`'da) reddediyor.
   Hâlâ yok: throughput %, PDU session failure, çağrı düşürme, kapsam
   `affected_areas/ues`. Bilinçli dışarıda: PCI çakışması (hücre id RRC'nin map
   anahtarı — gerçek semptom yerine simülatör çökmesi üretir) ve RRC reject
   (her UE t=0'da bağlanıyor, sonradan reddedecek bağlantı yok).
4. **Zamanlanmış RIC aksiyon script'i**: `RICActions[]` her biri `delay: "2.0m"`
   ile senaryonun parçası. Bizde kontrol yalnız interaktif/xApp üzerinden.
5. **Çok bant / CA / MU-MIMO**: LTE+NR karışık bant kütüphanesi, UE başına
   `num_antennas`/`extra_ca_carriers`/`mimo_capability`. Motor sınırı — kısa
   vadede kapanmaz.
6. **Alan/zon kavramı + üretici topoloji**: isimli `a1..an` zonları, UE grubunun
   zonlara dağılımı (`distribution[]`), ASCII diyagram / OSM / random şehir
   üretimi. Bizde `topology.py` var ama config'in parçası değil.
7. **Referans app emülasyonu** (TS `high_watermark`/`ho_backoff`, Energy Saving
   `low/high_prb_usage`) — AI'ın yenmesi gereken hazır baseline.
8. **Düzenlenebilir 5QI karakteristikleri** (`resourceType`, `priority`,
   `window`, `MaxGbr`) ve `gbrTput` tabanı. Bizdeki 21 kalemlik katalog sadece
   etiket.
9. **Zaman takvimi** (gece/gündüz oranları, çok günlük dataset) ve **Import**
   (CSV/O1/NETCONF'tan hücre listesi).
10. TDD frame/multiplexing, O1/VES, güvenlik (IPSec/TLS/E2 IDS) — bilinçli
    kapsam dışı.

### Bize göre Tera5G'nin eksikleri

1. **PHY/MAC/RLC derinliği**: yukarıdaki ~100 alanlık yüzey Tera5G'de kapalı;
   `ns3_version: v2/v3` dışında dokunulamıyor.
2. **Metrik, açık geometri**: her hücre/bina tam koordinat + 3D kutu. Onlarda
   yerleşim ASCII diyagram/OSM/random üretimle — tek tek metre veremiyorsun.
3. **Hücre başına mMIMO dizi config'i** (`rows/cols/spacing/polarization`).
4. **Uyku dinamiği config'te** (derinlik + timer + uyanma gecikmesi + kontrolcü
   eşikleri). Onlarda Watt tablosu daha zengin, dinamik daha sığ.
5. **AI ile doğal dilden config üretimi**, şema doğrulama, geometri/zarf
   guard'ları, "hangi kritik parametre default kaldı" incelemesi.
6. **Kanıt katmanı**: `fidelity_report.py` + `param_audit.py`. Tera5G'de bunun
   karşılığı yok — kullanıcı düğmenin çalıştığına inanmak zorunda.

### Genel hüküm

Config *derinliği* artık ayıp değil: kıyaslanabilir 179 parametrelerine karşı
165, ve bizimkinin hangisinin gerçekten motora indiği **ölçülü**. Fark artık
parametre sayısında değil, **koşunun etrafındaki üründe**: kampanya, baseline,
eşik/verdict, rapor, dışa aktarım (eksik listesinin 1., 4., 7. maddeleri).
Sıradaki yatırım oraya gitmeli; config tarafında en yüksek getirili iki iş
2. (bant başına HO eşikleri) ve 3. (anomali çeşitliliği).
