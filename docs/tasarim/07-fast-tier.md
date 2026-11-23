# 7. Fast tier (Tier B)

## Fikir: hız zaman adımından gelir

Slow tier bir KPM raporunun kapsadığı ≈1 saniyelik havayı µ=2'de **4000 TTI adımıyla**
simüle eder. Tier B aynı raporun sayaçlarını **tek adımda**, kapalı formda hesaplar.
Hızın kaynağı budur. Kanal hesabını tamamen çıkarmak slow tier'da yalnız 1.34×
kazandırır; zaman adımını değiştirmek ise binlerce kat.

İki kural modeli dürüst tutar:

1. **Motorun parametrelerinden kurulur, konfigürasyondan değil.** Tier B,
   `main.nsoran_params()`'ın çıktısını alır. Bu, ns-3'ün aldığı parametrelerin
   aynısıdır: aynı kıstırmalar, aynı ortalama, aynı itilen UE'ler. Geometri tanım
   gereği aynıdır; iki katman arasındaki fark model hatası olarak okunabilir.
2. **Sabitler standarttan değil motorun kaynağından okunur** ve kodda kaynağıyla
   birlikte yazılır. Örnek: TB boyutu `MmWaveLteMiErrorModel::GetPayloadSize`'dan,
   AMC tabloları `MmWaveAmc`'den, RU akımları `MmWaveRadioEnergyModelEnb`'den.

Kareler slow tier köprüsünün ürettiği kareyle **aynı biçimdedir**. Aynı özetleyici,
aynı kriter ve aynı arayüz iki katmanı da okur.

## Nerede kullanılır

- **Kampanyalar**, `tier: "fast"` ile (`main._run_fast_tier` → `tier_b.stream`).
- **RIC Test'in analitik konsolu**: `ric_sim.py`, bağlantı durumunu
  `tier_b.link_state`'ten, hücre penceresini ve gücünü `window_model`'den alır.
- Gerçek RIC ile E2AP üzerinden, **araçlarla** (üründe değil), aşağıya bakın.

## Ne modelleniyor

Modül docstring'lerinde tarihi eski bir "NE YOK" listesi var; aşağıdaki liste
2026-09-23 itibarıyla güncel durumdur. Doğrulayan testler `tools/test_fast_tier.py`
içinde.

| Alan | Durum |
|---|---|
| **Zamanlayıcı** | Pencere başına sembol bütçesi. Akış başına en az 1 sembol, **slot başına en çok 6 UE** (`MAX_UES_PER_SLOT`, motorun `MmWaveFlexTtiMacScheduler`'ından). `fixed_tti` + `sym_per_slot` tek kaldıraç. |
| **Slice** | Tavan: açgözlü, dönüşümlü doldurma. Taban (`min_pct`): korumasız slice'ların talebini küçültür (motor gibi hiçbir şey ayırmaz). `dedicated_pct` tabanla örtüşür, üstüne binmez. |
| **Link bütçesi** | 3GPP UMa/UMi yol kaybı; LOS/NLOS durumu **her 100 ms'de yeniden çekilir** (`COND_UPDATE_MS`, motorun `channelCondUpdateMs` varsayılanı). gNB 8×8 UPA ve UE 2×2 UPA kazancı (Dirichlet çekirdekleriyle hedef dışı yönlerde de). TR 38.901 anten elemanı: azimut ve eğim gerçek birer kaldıraç. |
| **Girişim** | Spektral örtüşme oranıyla. `MaxLossDb = 110 dB` budaması **yalnız girişime** uygulanır; simetrik uygulamak 36 UE'nin 13'ünü düşürüyordu. |
| **Blok hatası** | Türetilmiş, uydurulmamış: 8.2 dB AMC marjı ve durum/ışın karışımından gelen SINR yayılımı. HARQ sonrası orandır. |
| **Mobilite** | Sınırlı, difüzif rastgele yürüyüş: 4000×4000 m kutu, 30 m bacaklar, kenarda yansıma. Motorun `RandomWalk2dOutdoorMobilityModel` ayarıyla aynı. Rota (waypoint) verilen UE'ler rotayı izler. |
| **Handover** | A3 ofseti + histerezis, A2, `qRxLevMin`, band önceliği (TS 38.304: 3.6 GHz > 700 MHz), L3 filtre (`rsrp_filter_coefficient`). Handover **servis eden hücrenin menzilden çıkması** olarak sayılır, sıralama değişikliği olarak değil. `ho_ttt_ms` yalnız slow tier'da. |
| **Enerji** | RU güç durumları motorun akımlarıyla; mikro/hafif/derin uyku. Boşta kalma süresi pencereler arasında taşınır (boşta = sembol yok). Hücre kapatma. `PEE.AvgPower` / `PEE.Energy` sayaçları. |
| **Arızalar** | Dört anomali türü; pencereyle **örtüşme** anında ateşlenir. |
| **Trafik** | Çağrı modeli fazlı görev döngüsü olarak; S1-U kapasitesi hücre başına teslim tavanı. |
| **Uplink** | Var. UE dizisi karşılıklılık gereği uplink'te de uygulanır. |
| **Çoklu bant** | Hücre başına taşıyıcı (frekans, bant genişliği, numerology). **ns-3 bunu yapamaz**: tek taşıyıcı koşar. Tek bantlı senaryolarda kareler bit bit öncekiyle aynıdır. |

## Ne modellenmiyor

`schema.TIER_SCOPED` hangi alanın hangi katmanca okunduğunu ve **nedenini**
tutar. Editörde bu alanlar "ANALYTICAL" rozetiyle görünür. Yapısal olarak
yalnız slow tier'da olanlar: pencere altı periyotlar (µs/ns), bağlanma prosedürü
(RA/RRC/T300), taşıma gecikmeleri, `ho_ttt_ms`, SVD çözücü, `harq_enabled`,
`amc_model` / `error_model`, `channel_update_ms` (küçük ölçekli yeniden çekim;
gölgeleme mesafeyle ilişkisizleşir, zamanla değil), `duration_s`, `kpm_collect_s`.

Motorun ölçülmüş MI hata modeli portu (`mi_error_model.py`) **kapalıdır**:
sezgisel yöntemin geçtiği bir doğrulamada başarısız oluyor.

## Doğruluk nasıl ölçülür

| Araç | Soru |
|---|---|
| `tools/tier_criterion.py measure / check` | Tier B, Tier A'nın **kendi yayılımı** içinde mi? Bant = tohum ortalamasının 2 standart hatası. Koşu uzunluğu bir girdidir, çünkü yayılım uzunluğa bağlı. |
| `tools/tier_breadth.py --report` | Referans ailesinin dışında (binalar, mobilite, slice kotası, dar bant, uyuyan hücre) oranlar |
| `tools/tier_agreement.py` | Katmanlar arası metrik metrik uyum |
| `tools/beam_mixture.py`, `tools/rx_trace.py` | SINR yayılımı, motorun iletim başına izine karşı (`nsg-engine:1.32-rxtrace` bu izi üretir) |
| `tools/test_fast_tier.py` | Davranış testleri; `verify.py --fast` içinde |

**Kriterin sözleşme metrikleri** (bandı bağlayıcı): PRB, gönderilen TB, TB hataları,
64QAM payı, kuyruktaki veri, UE sayısı, handover, blok hata oranı. **Yalnız
raporlananlar** (tohumlar arası çok oynak): UE başına downlink, PDCP bit hızı,
QPSK payı.

**Durum (2026-09-23, 6 hücre / 36 UE referansı):** bant dışında kalan dört metrik:

| Metrik | Tier B / Tier A |
|---|---|
| spektrum | +37.5% |
| UE sayısı | +34.6% (kısmen raporlama ritmi farkı) |
| handover | +40.4% |
| blok hata oranı | −52.4% |

Açık işler: binalı senaryolarda yayılımın yalnız yarısı yakalanıyor; eşzamanlılık
bağlaşımı (uygunluk hücreyi eksik abone bırakıyor). Ayrıntı:
[10](10-bilinen-sinirlar.md#fast-tier).

**Hız:** gerçek zamandan onlarca kat hızlı (senaryoya göre ölçümler ≈50–80×).
600 pencerelik (600 simüle saniye) koşular olağandır; slow tier bir koşuda 1
simüle saniyenin altında kalır.

## Uzun ufkun öğrettiği

600 pencerelik koşular, 2 saniyelik koşuların yapısal olarak gösteremediği dört
kusuru ortaya çıkardı: yürüyüş sahneden çıkıyordu, rotalar yok sayılıyordu,
kampanya kartı süreyi slow tier aritmetiğiyle hesaplıyordu, uyku zamanlayıcısı
her pencerede sıfırlanıyordu. Dördü de düzeltildi. **Yeni bir özellik eklerken uzun
bir koşuyla da deneyin.**

## Gerçek E2AP üzerinden

"İki katman da gerçek RIC'i ve gerçek E2AP'yi korur, yalnız altındaki RAN
değişir" iddiası **kanıtlandı**:

- `nsg_ai/e2_feed.py` her pencerede satır başına `<ölçüm adı> <imsi> <değer>` yazan
  bir dosya üretir. Dosya geçici bir ada yazılıp atomik olarak yeniden adlandırılır.
- `engines/flexric/nsg_feed.c`, FlexRIC'in emülatör ajanı içinden bu dosyayı okur.
  `engines/flexric/apply_nsg_feed.py` ajanı yerinde ve tekrarlanabilir biçimde
  değiştirir. Orijinaller `*.nsg-orig` olarak saklanır.
- Ajan → nearRT-RIC → üretimdeki değiştirilmemiş `xapp_kpm_rc`. xApp'in okuduğu
  değerlerin %100'ü Tier B'ninkiydi. Tier B'nin yayınlamadığı sayaç ise ajanın
  rastgele değeri olarak kaldı; kanıtı güçlü kılan kontrol grubu bu.
- Çalıştırma: `tools/tier_b_serve.py`, `tools/e2_spike.sh` (motor geliştirme
  ortamında). Sözleşme testi `tools/test_e2_feed.py`, `verify --fast` içinde.
- İsim eşlemesi (emülatörün beklediği ↔ Tier B'nin ürettiği) `e2_feed.py`'nin
  docstring'inde.
