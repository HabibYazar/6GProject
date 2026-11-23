# 6. Motor — slow tier (ns-O-RAN)

## Nedir

Gerçek bir 5G yığını: EURECOM **FlexRIC** (near-RT RIC) + **ns-3.42 mmWave** RAN
(ns-O-RAN çatalı, `mmwave-LENA-oran`). RAN, **e2sim** üzerinden gerçek bir E2 ajanı
gibi davranır; RIC ile gerçek **E2AP / SCTP** üzerinden **E2SM-KPM v3** ve
**E2SM-RC** konuşur. Sayaçları tahmin etmez, TTI TTI simüle ederek **ölçer**.

## İki imaj, iki amaç

| İmaj | Boyut | Ne için |
|---|---|---|
| `nsg-engine:<etiket>` (şu an `1.32-rxtrace`) | ≈1.5 GB | **Çalışma zamanı.** Yalnızca binary'ler ve ldd ile bulunmuş birkaç kütüphane. Compose'daki `engine` servisi. |
| `nsg-engine-base:20.04` | ≈100 MB | Çalışma zamanı imajının **tabanı**: Ubuntu 20.04 + 4 paket + `builder` kullanıcısı. Ağ gerektiren tek parça. Motor imajı bunun üstüne **ağsız** derlenir. |
| `nsg-engine-dev:20.04` | ≈16 GB | **Geliştirme ortamı**: motorun derlendiği Ubuntu 20.04 dağıtımının tamamı (derleyiciler, bağımlılıklar, ns-3 ve FlexRIC kaynak + derleme ağaçları). Bkz. [08](08-gelistirme.md#motor-geliştirme). |

## Geliştirme ortamındaki yerleşim

| Yol | İçerik |
|---|---|
| `/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran/` | ns-3.42 mmWave kaynak ağacı (39 yama uygulanmış). `./ns3 build`. |
| `…/mmwave-LENA-oran/scratch/scenario-zero-with_parallel_loging.cc` | **Senaryo**: platformun her parametresini alan tek ns-3 programı |
| `…/mmwave-LENA-oran/build/` | Derleme çıktısı (`default` profil). Senaryo binary'si: `build/scratch/ns3.42-scenario-zero-with_parallel_loging-default` |
| `/home/builder/ns-O-RAN-flexric/e2sim-kpmv3/` | E2 simülatör kütüphanesi (RAN'ın E2 ajanı tarafı) |
| `/root/nsoran/` | Yapıştırıcı script'lerin kopyası + `flexric/` (kaynak ve derleme) |
| `/usr/local/lib/flexric/`, `/usr/local/etc/flexric/flexric.conf` | FlexRIC servis modelleri ve yapılandırması |

ns-3 ve senaryo binary'si **root olarak çalışmayı reddeder**; `builder`
kullanıcısıyla çalıştırın.

> **Derleme profili tuzağı.** Ürün `default` profille derlenmiş binary'yi yükler.
> `-d optimized` ile derlemek hızlı görünür ama platformun beslendiği her
> `[NSG-*]` günlük satırını sessizce yok eder; `optimized --enable-logs` ise yalnız
> 1.36× kazandırır. Farklı profilde bir derleme, ürünün hiç yüklemediği bir
> kütüphaneye gider ve bunu yalnız zaman damgaları gösterir.

## Servisler

| Port | Program | Ne yapar |
|---|---|---|
| `8090` | `engine_service.py` | `GET /health`; `POST /run` → `run_scenario.sh` çalışır, bloklar, toplu JSON sonuç döner (`status`, `ended_by`, `energy.span_s`, `measurements`) |
| `8091` | `live_bridge_server.py` | Canlı oturum: `run_live_session.sh`'i başlatır, xApp'in KPM çıktısını satır satır akıtır, gelen kontrol satırlarını FIFO üzerinden köprü xApp'e iletir |

Bir motor örneği **aynı anda tek koşu** yapar: script'ler sabit `/tmp`
yolları ve `/tmp/nsg_engine.lock` kullanır. Paralellik için örnek sayısını
artırın (compose `pool` profili, bkz. [09](09-kurulum-ve-isletim.md#kampanya-için-ek-motor)).

## Bir koşu içeride nasıl ilerler

1. Backend `nsoran_params(config)` ile konfigürasyonu **motorun parametrelerine**
   çevirir: sayıları zarfa kıstırır, sahneyi 4000×4000 m çerçeveye ortalar, UE'leri
   binalardan ve istasyonların üstünden iter.
2. Script parametreleri ortam değişkenlerinden alıp RIC'i, ns-3 RAN'ı
   (`--gnbPositionsFile`, `--uePositionsFile`, `--buildingsFile` … gibi argümanlarla)
   ve KPM xApp'ini başlatır.
3. xApp, `settings.kpm_collect_s` süresince KPM toplar. Bu süre **ufkun
   kendisidir** (duvar saati → simüle zaman, doğrusal takas).
4. Çıktı ayrıştırılır; hücre ve UE bazında sayaçlar backend'e döner.

## Günlükler

Bir koşu boş (`no_data`) ya da tuhaf dönerse **önce** motor container'ında
`/tmp/scn0.log` dosyasına bakın. **Son satırı ns-3'ün tam komut satırıdır.**
Şemanın izin verdiği ama motorun argüman denetleyicisinin reddettiği bir değer,
ns-3'ün ayrıştırma anında çıkmasına ve çıplak bir `no_data` dönmesine yol açar.
Model kaynağından akıl yürütmeden önce bu satırı okuyun.

```bash
sudo docker compose exec engine tail -5 /tmp/scn0.log
```

## Çalışma zarfı

`nsg_ai/engine_envelope.py` tek doğruluk kaynağıdır. Hem backend kıstırmasını
hem LLM talimatını besler.

| Sınır | Değer |
|---|---|
| Hücre (gNB) | 1 – 16 |
| UE toplam | 1 – 150 |
| UE / hücre | 2 – 30 |
| Bant genişliği | 10 – 50 MHz |
| Frekans | 600 MHz – 60 GHz |
| Hücreler arası en az mesafe | 50 m |

Zarfın dışında motor bozulur (PRB 0 okunur) ya da çöker. Zarfı genişletirseniz
`tools/envelope_sweep.py --via-api` ile ölçün. Çıplak binary trafik kurmadığı için
ürünün çöktüğü noktaları "geçer" gösterir.

## Hız

- ns-3 her TTI'yı işletir. µ=2'de saniyede 4000 adım. Maliyet **UE başına**:
  30 UE'de ≈0.0075, 150 UE'de ≈0.0006 simüle-sn / duvar-sn ölçüldü.
- Hızlandırma kaldıraçları **ölçülerek kapatıldı**. En iyi tek konfig kaldıracı
  ≈1.3×; hepsi birlikte de ≈1.3× (üst üste binmiyorlar). Kanalı tamamen çıkarmak
  bile yalnız 1.34× kazandırır ve profil düzdür (hiçbir fonksiyon %7'yi geçmez).
- Bir koşu **tek çekirdek** kullanır. Çekirdekler koşu hızını değil kampanya
  verimini artırır: altı eşzamanlı koşu ≈5.8× verim, koşu başına ≈%4 yavaşlama.
- Uzun ufuk ya da büyük tarama gerekiyorsa fast tier'ı kullanın
  ([07](07-fast-tier.md)).

## Motor kodu üç yerde yaşar

```
repo: engines/nsoran/         →  derleme ortamı: /root/nsoran/       →  imaj: nsg-engine:<etiket>
(düzenlediğiniz yer)              (build-slim.sh'in paketlediği yer)       (compose'un çalıştırdığı)
```

C++ değişiklikleri ise derleme ortamındaki ns-3 ağacında yapılır ve **yama
olarak** repoya geri yazılır. Hiçbir adım kendiliğinden eşitlenmez; kayma
sessizdir. Ürüne ulaşmanın **tek yolu yeni bir imaj** ve compose'un o etiketi
kullanmasıdır. Adım adım: [08](08-gelistirme.md#motor-geliştirme).

## Yamalar

`engines/nsoran/*.patch`, ns-3 ağacında `mmwave-LENA-oran/` kökünden
`patch -p1` ile uygulanır. Geliştirme ortamındaki ağaçta **hepsi uygulanmış**
durumdadır. Sıfırdan bir upstream ağacı kurulacaksa bunlar ns-O-RAN'ın üstüne
uygulanır. Yamaların çoğu başında ne yaptığını ve neden yaptığını anlatan uzun
bir açıklama taşır; yama dosyasını açıp okumak en iyi dokümandır.

| Yama | İlk açıklama satırı (yamanın kendi başlığı) |
|---|---|
| `antenna-2c.patch` | NSG Phase 2c (antenna) — per-cell panel orientation, and the element model |
| `antenna-family-anomalies.patch` | NSG roadmap items 3 + 4 — the antenna element family, the rest of LteEnbRrc, |
| `beamforming-kpm.patch` | NSG — beamforming model, control-plane realism, and which KPM families the |
| `bursty-external-interference.patch` | (başlık yok) değişen dosya: `src/mmwave/model/mmwave-interference.cc` |
| `call-dynamics.patch` | NSG call/session dynamics — traffic that breathes instead of a constant faucet. |
| `channel-condition-period-knob.patch` | ns-O-RAN mmWave: the LOS/NLOS condition re-draw period was hardcoded. |
| `channel-rlc.patch` | NSG — channel realism (blockage, O2I penetration, channel ageing) + RLC, |
| `component-carrier-ubfix.patch` | (başlık yok) değişen dosya: `src/lte/model/component-carrier.cc` |
| `energy-control.patch` | NSG live cell on/off (Energy_state) — ns-3 mmWave module change. |
| `error-model.patch` | NSG — link-level error model, S1-AP control plane, PHY/scheduler detail. |
| `external-interference-noise-floor.patch` | (başlık yok) değişen dosya: `src/mmwave/model/mmwave-ue-phy.cc` |
| `flex-tti-zero-pdu.patch` | MmWaveFlexTtiMacScheduler: the transport-block split aborted the run, and hid |
| `geometry-mro.patch` | NSG — node heights, antenna spacing, cell selection, the DynamicTtt curve and |
| `inert-fields.patch` | NSG — the config fields the UI accepted and the engine ignored. |
| `kpm-collection-window.patch` | FlexRIC kpm_rc xApp: the KPM collection window was a hardcoded sleep(5), and |
| `kpm-empty-record-guard.patch` | (başlık yok) değişen dosya: `contrib/oran-interface/model/kpm-indication.cc` |
| `kpm-neighbour-align.patch` | (başlık yok) değişen dosya: `contrib/oran-interface/model/kpm-indication.cc` |
| `longterm-beam-cache.patch` | ns-O-RAN / ns-3 spectrum: cache the long-term beamforming component PER BEAM. |
| `mobility-2b.patch` | NSG Phase 2b — mobility & handover: waypoint paths + 3GPP measurement events. |
| `mro-layer.patch` | NSG MRO layer — real mobility + handover-margin plumbing, honestly scoped. |
| `multi-service.patch` | NSG multi-service per UE — a group's services list is now REAL traffic. |
| `nr-kpm-spike.patch` | NSG spike — can 5G-LENA `nr` talk to our RIC? Partly, and now we know the cost. |
| `phy-mac.patch` | NSG — PHY/MAC on the mmWave DATA PATH. Chosen over ICIC, and why that matters. |
| `prb-truncation-fix.patch` | NSG fix: RRU.PrbUsedDl zeroed out in high-MCS / light-load regimes |
| `radio-2c.patch` | NSG Phase 2c (radio) — AMC + HARQ shipped; scheduler, TDD split, MIMO and |
| `rar-rnti-hijack-guard.patch` | ns-O-RAN mmWave: two UEs could end up sharing one RNTI on one cell. |
| `rsrp-from-received-psd.patch` | ns-O-RAN mmWave: compute RSRP. SHIPPED in nsg-engine:1.25. |
| `scenario-buildings-level4.patch` | NSG Level-4 — real buildings in the ns-O-RAN engine. |
| `scenario-slices-level5.patch` | NSG Level-5 — per-UE slice traffic in the ns-O-RAN engine. |
| `scenario-zero-level2.patch` | (başlık yok) değişen dosya: `scratch/scenario-zero-with_parallel_loging.cc` |
| `scenario-zero-level3.patch` | (başlık yok) değişen dosya: `scratch/scenario-zero-with_parallel_loging.cc` |
| `sleep-state-2a.patch` | NSG Phase 2a — RU sleep states (deep / light / micro) on the real ns-O-RAN engine. |
| `slice-depth.patch` | NSG roadmap item 2 — slice and traffic depth in the engine. |
| `slicing-level6.patch` | NSG Level-6: REAL network slicing — per-QCI resource partitioning in the |
| `spectrum-ceiling-and-beam-cache.patch` | (başlık yok) değişen dosya: `src/spectrum/model/three-gpp-spectrum-propagation-loss-model.cc` |
| `srb0-path-miss-guard.patch` | (başlık yok) değişen dosya: `src/mmwave/helper/mmwave-bearer-stats-connector.cc` |
| `transport-o2i.patch` | NSG — transport network (backhaul), indoor/outdoor channel condition, PHY |
| `ul-reporting.patch` | NSG UL reporting — the engine is no longer DL-only. |
| `zero-window-report-fix.patch` | NSG fix: zero-counter KPM reports (PRB/UL read 0 while traffic flows) |

## Enerji modeli

Hücre başına 1–3.7 kW okumak hata değildir: güç = akım × 5 V besleme, upstream
ns-O-RAN'ın akım varsayılanlarıyla (TX 742.2 A, RX 138.9 A, boşta 86.3 A; uyku
mikro/hafif/derin 59.5 / 43.2 / 28.5 A). Üç akım da şema alanıdır. Enerji
karşılaştırmalarında **Joule değil Watt** kullanın; koşuların simüle süresi farklıdır.

## Motorun bilinen kusurları

Rastgele tohuma bağlı deterministik bir çökme, yanlış hesaplanan beş KPM sayacı,
hücre başına handover eşiği kurulamaması gibi konular:
[10-bilinen-sinirlar.md](10-bilinen-sinirlar.md#motor-slow-tier).
