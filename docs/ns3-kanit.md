# ns-3 kanıtı — upstream ns-O-RAN'ın kendi arayüzünde koşu

> Müşteri "motorunuz gerçekten ns-3 mi?" diye sorduğunda, **bizim** arayüzümüzde
> bizim sayılarımızı göstermek kanıt değildir. Bu doküman, upstream ns-O-RAN'ın
> **kendi panosunda, kendi hazır senaryosunu** koşturmanın adımlarını ve neyin
> kanıtlanıp neyin kanıtlanmadığını yazar.
>
> Son doğrulama: 2026-09-18, canlı.

## 1. Ayağa kaldırma

Motor distrosunda, root olarak:

```bash
wsl -d Ubuntu-2004-nsoran -u root bash /home/chancelor/nsg_ai_platform/tools/nsoran_gui_up.sh
```

| Servis | Adres | Not |
|---|---|---|
| Upstream ns-O-RAN panosu | http://localhost:8010 | senaryo seçimi + Start/Stop |
| Grafana | http://localhost:3001 | `admin` / `admin` |
| InfluxDB | http://localhost:8086 | ham seriler |

Pano boşsa veri ~40–60 sn sonra dolar.

## 2. Kanıt zinciri

Bu yolun hiçbir adımında bizim kodumuz yok:

```
:8010 panosu  →  POST /start_simulation
              →  gui_trigger.py   (host, `builder` kullanıcısı, :38866)
              →  ./ns3 run        (upstream'in kendi build sistemi)
              →  build/scratch/ns3.42-<senaryo>-default
              →  E2AP  →  sim_data_pusher.py  →  InfluxDB  →  Grafana
```

Panodaki "Start new" düğmesinin gönderdiği POST ile yukarıdaki komut aynıdır.

## 3. Ağacın kimliği — ekranda gösterilecek üç komut

```bash
D=/home/builder/ns-O-RAN-flexric/mmwave-LENA-oran
git -C $D remote -v      # https://github.com/MinaYonan123/mmwave-LENA-oran.git
git -C $D log --oneline -1
cat $D/VERSION           # 3.42
```

2026-09-18 itibarıyla:

- **remote:** `https://github.com/MinaYonan123/mmwave-LENA-oran.git` (herkese açık)
- **upstream commit:** `0ae720c9 PR: integrate sionna-rt module and 5G LENA nr
  module with updated version of ns3-mmwave (#20)`
- **VERSION:** `3.42` — ns-3'ün kendi sürüm dosyası

## 4. Bizim değişikliklerimiz — saklanmıyor, sayılıyor

```bash
git -C $D status --short
git -C $D diff --shortstat     # 32 files changed, 3060 insertions(+), 217 deletions(-)
```

Ağaç **upstream + bizim yamalarımız**. Bunu saklamak yerine göstermek kanıtı
güçlendirir: ortada yeniden yazılmış bir simülatör değil, gerçek bir ns-3
ağacına yapılmış sayılabilir eklemeler var. Koşu logunda görünen `[NSG-RSRP]`
satırları bizimdir — RSRP yayan yama (`engines/nsoran/rsrp-from-received-psd.patch`).

`NSG-` işareti taşıyan kaynak dosyalar: `mmwave-enb-phy.cc`, `lte-enb-rrc.cc`,
`nr-gnb-net-device.cc`.

## 5. Panodan koşan senaryolar — ÖLÇÜLDÜ

Panonun dropdown'ı 11 senaryo listeler ama formu **tek bir sabit argüman kümesi**
gönderir; her upstream senaryosu kendi argümanlarını kaydettiği için bir kısmı
ns-3'ün argüman ayrıştırmasında düşer. Demoda yanlışını seçmek "Invalid
command-line argument" ile biter.

Kaynağı okumak bunu **yanlış** söyledi (`scenario-one` sağlam görünüp `--simTime`'ı
reddetti), o yüzden her biri panodan başlatılıp ölçüldü:

| Senaryo | Panodan |
|---|---|
| `scenario-zero-with_parallel_loging` | **KOŞAR** — panonun varsayılanı, panoları en iyi dolduran |
| `Energy_saving_with_cell_utilization_scenario` | **KOŞAR** |
| `Energy_Saving_with_load_balancing_scenario` | **KOŞAR** |
| `orange-rf-channel-reconfiguration` | **KOŞAR** |
| `scenario-zero` | düşer — `--N_MmWaveEnbNodes` yok |
| `scenario-one` | düşer — `--simTime` yok |
| `scenario-three`, `rf-channel-reconfiguration-scenario`, `cttc-nr-demo`, `scratch-simulator`, `simple-sionna-example` | formun argümanlarını almıyor |

**Demoda dropdown'dan rastgele seçme.** Yukarıdaki dört taneden birini seç.

## 5b. Formda neye dokunma — ÖLÇÜLDÜ

Sayısal alanlar serbest: hepsi `--<alan>=<değer>` olarak ns-3'e geçer ve senaryo
bunları kabul eder. `N_Ues` 3 → 8 denendi, sorunsuz koştu. Dokunulmayacak **üç**
şey var, üçü de koşarak doğrulandı:

| Kontrol | Doğru hâli | Yanlış hâlinde |
|---|---|---|
| **Scenario flags** | ☑ **işaretli** | ns-3 hiç başlamaz; komut argümansız kurulur ve pano 0 UE / 0 hücre gösterir |
| **Connect to FlexRIC** | ☐ **boş** | `--enableE2FileLogging=1` düşer, ayrı bir FlexRIC bekler; ns-3 ölür, pano boş kalır |
| **Scenario** | bölüm 5'teki dörtten biri | ns-3 argüman ayrıştırmada düşer |

Sayısal alanlarda da iki sınır var, ikisi de bizim kendi motorumuzda görülen
sınırların aynısı (aynı ağaç):

- `indicationPeriodicity` **0.01 – 2.0** arası. `5` denendi:
  `Invalid command-line argument: --indicationPeriodicity=5`, ns-3 başlamadı.
- `Bandwidth` / `N_Ues`: UE başına ~2 MHz'in altında flex-TTI zamanlayıcı abort
  eder. 20 MHz'de `N_Ues` 10'un altında kalsın.

### Panonun UE/hücre sayacı yalan söyleyebilir

Sayaçlar **formdan** hesaplanıyor (`Simulation(number_of_ues, number_of_cells)`),
simülasyondan değil. FlexRIC kutusu işaretliyken ns-3 hiç başlamadığı hâlde pano
"3 UE, 5 hücre" göstermeye devam etti. **Grid dolu görünmesi koşunun yaşadığını
kanıtlamaz** — bölüm 6'daki zaman damgası ya da şu tek satır:

```bash
pgrep -cf 'build/scratch/ns3.42-'     # 1 olmalı
```

## 6. Canlı olduğunu kanıtlama — dolu pano yetmez

InfluxDB eski koşuların verisini tutar; dört günlük veri de tıpatıp aynı panoyu
çizer. Kanıt için **zaman damgası** gerekir:

```bash
curl -s --get http://localhost:8086/query --data-urlencode "db=influx" \
  --data-urlencode 'q=SELECT * FROM "ue_2_l3 serving sinr" ORDER BY time DESC LIMIT 2'
date -u +%Y-%m-%dT%H:%M:%SZ
```

Son satır ile şimdiki zaman arasında saniyeler olmalı. 2026-09-18 doğrulamasında
fark **2 saniyeydi**.

Grafana'nın kendisinin de okuduğunu göstermek için (panelin izlediği yol):

```bash
curl -s -u admin:admin --get http://localhost:3001/api/datasources/proxy/1/query \
  --data-urlencode "db=influx" \
  --data-urlencode 'q=SELECT last("value") FROM "du-cell-2_rru.prbuseddl"'
```

## 7. Grafana datasource tuzağı — DÜZELTİLDİ 2026-09-18

Datasource `http://influxdb:8086` adresini gösteriyordu; bu, upstream'in
docker-compose bridge ağındaki servis adı. Bu WSL'de bridge çalışmadığı için
container'lar **host ağında** koşuyor ve o ad **çözülmüyordu**. Sonuç: Grafana
sapasağlam görünürken bütün paneller boş. `http://localhost:8086` olarak
düzeltildi.

Container yeniden yaratılırsa geri gelir. Demo öncesi kontrol:

```bash
docker exec nsoran-grafana sh -c 'getent hosts influxdb || echo YOK'   # YOK beklenir
curl -s -u admin:admin http://localhost:3001/api/datasources | grep -o 'http://[^"]*8086'
```

İkincisi `http://localhost:8086` demeli.

## 7b. Bu koşu da yavaş — ve bu iyi bir şey

Aynı ns-3.42 ağacı, dolayısıyla aynı mertebede yavaş. **Ölçüldü** (3 UE, 4 mmWave
gNB + 1 LTE, panonun varsayılan formu, 191 s duvar içinde):

```
sim t: 1.76 -> 14.32   over 191 s wall
sim/wall           : 0.0658
x slower than real : 15.2x
```

Bizim motorumuzun aynı gün ölçülen noktaları: 3 hücre/3 UE **8.3x**, 3 hücre/6 UE
**24.7x**. Yani upstream'in kendi referans panosu da aynı bantta.

Birebir aynı değil, çünkü **komut satırları farklı**: biz
`--ns3::SpectrumChannel::MaxLossDb=110` ile yayılım budaması yapıyoruz, panonun
formu bunu hiç göndermiyor (doğrulandı: komutta yok). Bizde 5 yerine 3 hücre var.

**Demoda kullanılacak cümle:** *"ns-O-RAN'ın kendi referans panosu da gerçek
zamanın 1/15'inde koşuyor. Bu bizim eksiğimiz değil, tam yığın bir ns-3 RAN'ın
maliyeti."* Hız sorusu gelirse en iyi cevap budur — kendi ölçümümüz değil,
upstream'in kendi aracının ölçümü.

## 7c. Koşu gözetimsiz ÖLÜYOR — pencereyi açık tut

10:14'te başlatılan koşu, 13:17'de **hatasız** durmuş bulundu: logda ne assert ne
OOM var, satırlar sim t=12.6'da temiz biçimde kesiliyor. Üç saatte 12.6 simüle
saniye, ölçülen 0.0658 hızıyla açıklanamaz (sürekli koşsaydı ~700 s olurdu) —
yani çoğu zaman **hiç koşmamış**. Bu, WSL'in boştaki distroyu askıya alması.

Tek bir wsl.exe oturumu açık tutulduğunda 3 dakika boyunca kesintisiz koştu.

`nsg_demo_up.bat` artık **iki** distro için de keepalive tutuyor; o pencere açık
kaldığı sürece bu koşu da yaşar. Kapatma, küçültmekle yetin.

## 8. Bu kanıtın SINIRI — demoda söyle

Bu pano **upstream'in kendi simülasyonunu host ağacında** koşuyor, **bizim engine
konteynerimizin koşusunu değil**. İkisi aynı ns-3.42 ns-O-RAN ağacından gelir
(konteyner imajı bu ağaçtan derlenir) ama aynı proses değildir.

Söylenecek cümle: *"Bu, ns-O-RAN'ın kendi arayüzünde kendi senaryosu. Ürünümüzün
motoru aynı ağaçtan derlenmiş bir konteyner — aynı ns-3, ayrı proses."*

Bunu söylemek kanıtı zayıflatmaz; söylememek, fark edildiğinde kanıtın tamamını
götürür.

## 9. Kapatma

```bash
curl -s -X POST http://localhost:8010/stop_simulation
docker stop nsoran-gui nsoran-grafana nsoran-influx
```

Kendi motorumuz ayrı konteynerdir (`nsg-engine`), bunlardan etkilenmez — ama ikisi
aynı anda koşarsa CPU'yu paylaşırlar ve **ikisinin de** ölçümü bozulur. Demoda
sırayla göster.
