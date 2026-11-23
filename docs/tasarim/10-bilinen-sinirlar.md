# 10. Bilinen sınırlar, hatalar ve açık işler

Bir ölçüme güvenmeden önce bu sayfayı okuyun. Her madde, bulunduğu ve son
doğrulandığı tarihle birlikte yazıldı. Tarih eskiyse yeniden ölçün.

## Güvenlik

| Konu | Durum | Ne yapılmalı |
|---|---|---|
| **`POST /ric/validate-nsoran` anonim çağrıyı kabul ediyor** | Açık (2026-09-24 kodda doğrulandı). `require_user` yerine `current_principal` kullanıyor; zorunluluk açıkken bile giriş yapmamış biri gerçek motoru çalıştırabilir, sonuç sahipsiz kaydedilir. | Parametreyi `Depends(require_user)` yapın; `test_isolation.py`'ye bir kontrol ekleyin. |
| **OpenRouter anahtarı git geçmişinde** | `nsg_ai/.env` ilk commit'ten beri git tarafından izleniyor ve önceki geliştiricinin anahtarını taşıyor. | O anahtarı kullanmayın, geçersiz sayın. Kendi anahtarınızı paketteki `nsg_ai.env`'e yazın. `git rm --cached nsg_ai/.env` yapıp `.gitignore`'a ekleyin. |
| **Motor API'sinin kimlik doğrulaması yok** | Tasarım gereği: motor portları dışarıya yayınlanmaz, yalnız backend ulaşır. Bir zamanlar `extra_args` üzerinden kabuk komutu çalıştırılabildiği kanıtlandı; hem backend reddediyor hem script meta karakterleri reddediyor (`tools/security_probe.py`). | Motor portlarını asla yayınlamayın. |
| Platform yalnız `127.0.0.1`'e bağlı | Tasarım gereği | Dışarı açmadan önce yukarıdaki ilk maddeyi kapatın. |

## Kurulum ve altyapı

- **Backend `OPENROUTER_API_KEY` olmadan açılmaz.** `llm_agent` içe aktarılırken
  hata verir. İnternetsiz makinede anahtar geçersiz olabilir; yalnız AI özellikleri
  çalışmaz. İyileştirme: anahtar yoksa AI uç noktalarını 503 ile kapatıp geri
  kalanı açmak.
- **İnternet yoksa AI sohbet ve AI ile senaryo üretimi çalışmaz**: LLM'ler
  OpenRouter'da. Yerel bir LLM'e geçiş `llm_agent.py`'de LangChain `ChatOpenAI`'nin
  `base_url`'ünü değiştirmekle başlar (OpenAI uyumlu bir sunucu).
- **Partition (bölme) compose dağıtımında çalışmaz.** `partition_pool.py` ek
  motorları host üzerindeki bir script'le (`docker run`) açıyor; backend container'ı
  bunu yapamaz. Kampanya paralelliği için havuzu kullanın. Bölmenin doğruluk
  bedeli de var: throughput ≈2×, blok hatası %7.7 → %0; komşu hücre girişimi
  kayboluyor (2026-09-19).
- **RL eğitim kiti (`GET /ric/training-kit`) bozuk.** Zip'e giren `ric_sim.py`
  artık `partition_live.CELL_BASE` ve `main.nsoran_params`'ı içe aktarıyor; ikisi de
  zipte yok. Düzeltme: `nsoran_params`'ı web'den bağımsız bir modüle taşımak,
  `CELL_BASE`'i ayrı tutmak.
- **WSL'e özgü araçlar** bu makinede çalışmaz: [08](08-gelistirme.md#wsle-özgü-araçlar).
- **Eski uç noktalar:** `/live-metrics`, `/engine-status`, `/stop` artık var olmayan
  bir servise (`localhost:29501`) soruyor, boş / `offline` döner. Silinebilir.

## Motor (slow tier)

| Konu | Ayrıntı | Son durum |
|---|---|---|
| **Tohuma bağlı deterministik çökme** | 3 hücre / 6 UE, 3.6 GHz, 50 MHz, UMa, `kpm_collect_s=120`: `rngRun=3` ve `4` t≈0.6 sn'de `truncated` döner (1 ve 2 sağlıklı). Aynı assert (`MmWaveFlexTtiMacScheduler`, TB bölme) daha önce düzeltilmişti; düzeltme eksik çıktı. Ucuz ve tekrarlanabilir bir repro. | Açık (2026-09-16'da yeniden açıldı; 1.30+ image'larla yeniden ölçülmedi) |
| **Numerology hep 2** | `MmWavePhyMacCommon::SetNrNumerology` yalnız 2 ve 3'ü kabul ediyor (FR2 varsayımı). Konfigürasyon 1 dese de motor µ=2 koşar. 3.6 GHz FR1'dir; gerçek bir hücre µ=1 kullanırdı. | Bilinçli olarak kıstırılıyor (`engine_envelope.config_numerology`) |
| **Beş KPM sayacı yanlış** | `DRB.BufferSize.Qos` uint32 alttan taşma (≈4.29e9); `DRB.UEThpDl` bit ÷ RLC **gecikmesi** (zaman değil); `RRU.PrbUsedDl` PRB değil, sembol payı × 139 (100 MHz / 60 kHz'e sabit, upstream'in kendi TODO'su); `QosFlow.PdcpPduVolumeDL` iki başlıkta iki farklı nicelik; `DRB.PdcpSduBitRateDl` 40 karenin 2'sinde var. | Platform tarafında `kpm_health.py` ile kontrol altında (2026-09-21). Motor düzeltilmedi. Hacim için `QosFlow.PdcpPduVolumeDL` kullanın. |
| **`L3servingSINR` kullanılamaz** | Güvenilir SINR kaynağı `L1M.RS-SINR.Bin*` histogramı ve `nsg-engine:1.32-rxtrace`'in iletim başına izi. | — |
| **Gecikme metriği gürültü** | `DRB.PdcpSduDelayDl` aynı konfig ve tohumda 6 koşuda 14× oynadı (throughput 1.1×). 4–5 örnek, ilki bağlanma tamponunu taşıyor. | Karar metriği olarak kullanmayın |
| **Hücre başına handover eşiği yapılamaz** | Tek LTE ankor RRC tüm handover kararlarını veriyor (`nLteEnbNodes = 1`); eşikler onun özniteliği. | Mimari sınır, eksik özellik değil |
| **Canlı kare bağımsız gözlem değil** | Her E2 mesajı bir kare üretir, ama raporu yalnız CU-CP taşır. Hücre başına ≈11× aşırı örnekleme. Kontratta `sim_t`, `kpm_fresh`, `kpm_fresh_cells` var. | İstatistikte taze kareleri sayın |
| **Toplu koşunun ufku kısa** | Bir koşu 1 simüle saniyenin altında görür; KPM penceresi ufkun kendisidir. `start_s=5`'te planlanan bir anomali hiç ateşlenmez. | Tasarım gereği. Uzun ufuk için fast tier. |
| **Enerji kW ölçeğinde** | akım × 5 V, upstream akım varsayılanları. Hata değil. Watt ile karşılaştırın. | — |
| **`MaxLossDb = 110 dB`** | ns-3'ün bağlantı budama eşiği; altında pay yok. | — |
| **Hız** | Tüm kaldıraçlar ölçüldü ve kapatıldı; gerçek zaman ≈26× uzakta. | Kapalı. Paralellik ve fast tier. |

## Fast tier

- **Bant dışında dört metrik** (2026-09-23, 6 hücre / 36 UE): spektrum +%37.5,
  UE sayısı +%34.6 (kısmen raporlama ritmi), handover +%40.4, blok hata oranı
  −%52.4. Ayrıntı: [07](07-fast-tier.md#doğruluk-nasıl-ölçülür).
- **Binalar:** SINR yayılımının yalnız yarısı yakalanıyor.
- **Eşzamanlılık bağlaşımı:** uygunluk (availability) hücreyi eksik abone bırakıyor.
  UE'lerin eşzamanlılığı erişim üzerinden bağlamak denendi, bayt teslimini %92
  aştırdığı için geri alındı.
- **MI hata modeli portu kapalı** (`mi_error_model.py`): sezgisel yöntemin geçtiği
  bir doğrulamada başarısız.
- Yapısal olarak modellenmeyenler: `schema.TIER_SCOPED`.

## Veri

- **2026-09-24 öncesi canlı koşular sahipsiz.** `/sim/run` biten koşunun kaydında
  `owner_id`'yi düşürüyordu (commit `f61bc43` ile düzeltildi). Eski veritabanındaki
  182 `live` koşu sahipsiz: yalnız admin görür. İstenirse `runs.owner_id`
  `ai_interactions` ya da kampanya kayıtlarından geri doldurulabilir.

## Eskimiş dokümantasyon (kod doğru, açıklama eski)

- `nsg_ai/tier_b.py` modül docstring'indeki "WHAT THIS TIER DOES NOT HAVE" listesi
  (mobilite yok, handover sıfır, NLOS yok): hepsi sonradan eklendi. Güncel liste
  [07](07-fast-tier.md#ne-modelleniyor).
- `POST /run-simulation` docstring'i harici bir binary anlatıyor; hesap artık süreç içi.
- `docs/SISTEM.md`, kök `README.md`, `engines/README.md`: motor etiketleri ve WSL
  komutları eski ([00](00-README.md#repodaki-diğer-dokümanlar--ne-kadar-güncel)).
