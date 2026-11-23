# Demo kitapçığı — pilot/ortak görüşmesi

> Karşımızdaki müşteri değil, pilot ortak. Yani satış yapmıyoruz: **ne çalışıyor,
> ne çalışmıyor, nereye gidiyoruz** — üçünü de söylemek en güçlü konum. Bu
> kitapçık sırayı, prompt'u ve gelecek soruların cevaplarını içerir.
>
> Hazırlandı ve **koşularak doğrulandı**: 2026-09-18, `nsg-engine:1.25`.

---

## 0. Başlamadan — 3 dakika

```bash
nsg_demo_up.bat          # pencereyi AÇIK bırak (iki distroyu da canlı tutuyor)
```

```bash
cd /home/chancelor/nsg_ai_platform && venv/bin/python tools/verify.py
```

`VERIFY OK` görmeden başlama. Görmezsen `bash tools/demo_app_up.sh`.

ns-3 kanıtı da gösterilecekse ayrıca: [ns3-kanit.md](ns3-kanit.md). **İkisini
aynı anda koşturma** — CPU'yu paylaşırlar, ikisinin de sayıları bozulur.

---

## 1. Demo prompt'u

NSG Assistant'a yapıştırılacak metin. Parametre listesi değil, **niyet** —
iş üretenin AI olduğunu gösteren şey bu.

```
A three-cell site at a city intersection. Two cells face each other across the
junction and a third sits back from it, covering the shadow the first two leave.
Six users on site: four stationary, and two in a vehicle that drives through the
junction and must hand over between cells while it does. The vehicle pair are on
a low-latency slice; the rest are ordinary mobile broadband.

The run has to be long enough that the handover actually happens. Compute the
horizon from the collection window and size the vehicle's path to it — do not
estimate it in prose, and put the arithmetic in the description. Choose a
bandwidth that matches the crowd.
```

Türkçe konuşulacaksa aynı niyet:

```
Şehir içi bir kavşakta üç hücreli bir saha. İki hücre kavşağın iki yakasında
karşılıklı, üçüncüsü geride durup ilk ikisinin bıraktığı gölgeyi kapatıyor.
Sahada altı kullanıcı: dördü sabit, ikisi kavşaktan geçen bir araçta ve geçerken
hücre değiştirmek zorundalar. Araçtakiler düşük gecikmeli bir dilimde, diğerleri
normal mobil genişbant.

Koşu, el değiştirmenin gerçekten gerçekleşeceği kadar uzun olsun. Ufku toplama
penceresinden HESAPLA ve aracın yolunu ona göre boyutlandır — düzyazıda tahmin
etme, aritmetiği açıklamaya yaz. Bant genişliğini kalabalığa göre seç.
```

### Bu prompt neyi gösteriyor

Tek bir paragraf, modelin **yedi ayrı çıkarım** yapmasını gerektiriyor:

| İstenen | Modelin yapması gereken |
|---|---|
| "karşılıklı iki hücre + geride üçüncü" | Geometriyi kurmak, hücreleri ayrı konumlandırmak |
| "gölgeyi kapatıyor" | Üçüncü hücrenin azimutunu kuzeye çevirmek |
| "araç geçerken el değiştirsin" | Yolun geçiş noktasını kesmesini sağlamak |
| "ufku hesapla" | Pencere → kare → simüle saniye → metre zincirini kurmak |
| "düşük gecikmeli dilim" | uRLLC dilimi + QCI ataması |
| "bant genişliğini kalabalığa göre" | UE başına ≥2 MHz kuralını uygulamak |
| (söylenmeyen) | Anten paneli, HO eşikleri, yayılım senaryosu, güç profili |

### Ölçülen sonuç (2026-09-18)

```
status      ok
duvar saati 65 s
UE          6          hücre 3        bant 20 MHz
KPM ismi    43
handover    3–6        (aynı config, iki koşu — sayı sabit değil)
DRB.UEThpDl mean 6.2 Mbps    RRU.PrbUsedDl mean 13
RSRP        15.142 örnek, -141 … -78 dBm
```

**65 saniye.** Demo için ideal. Daha çok handover istersen `kpm_collect_s`'i
60 yap: 98 saniye, 9 handover.

### Bu prompt'ta bilerek bırakılan tuzak — ve onu nasıl kullanacaksın

Model açıklamasında *"kpm_collect_s=30 → ~120 pencere → ufuk 9.2 s"* diye bir
aritmetik yazdı. **Koşu 1.3 s bildirdi.** Model kendi kuralını uyguladı ama
kural bu topolojide fazla iyimser.

Bunu saklama, **öne çıkar**:

> *"Modelin ufuk hesabı 9.2 saniye dedi, motor 1.3 ölçtü. Fark burada görünüyor
> çünkü sistem kendi ufkunu ölçüp raporluyor. Bu alanda en sinsi hata, hiç
> gerçekleşmemiş bir olayı 'test edildi' diye raporlamaktır — biz onu görünür
> yaptık."*

Bu, pilot bir ortağa söylenebilecek en güçlü şey: aracın **kendi üreticisini
denetlediğini** gösteriyor.

---

## 2. Demo akışı — 25 dakika

| # | Süre | Ne | Nerede |
|---|---|---|---|
| 1 | 3 dk | Prompt'u yapıştır, config'in doğmasını izle | NSG Assistant |
| 2 | 3 dk | Canvas + Map: hücreler, yol, binalar | Visual |
| 3 | 2 dk | Config editörü: 169 alan, AI'ın seçtikleri | Config |
| 4 | 5 dk | **Gerçek motor, canlı oturum** — KPM akıyor | RIC Test → Real engine |
| 5 | 4 dk | Canlı kontrol: hücre uyut, handover zorla, dilim kotası, arıza enjekte | RIC Test → Control |
| 6 | 3 dk | Compare: iki konfig, gerçek veri üstünde | Compare |
| 7 | 5 dk | Yol haritası + dürüst sınırlar | konuşma |

**4. adımda söylenecek:** başlangıçta tablolar boş görünür. Artık ekranda
*"First 0.30 s of simulated time — UEs are still attaching"* yazıyor ve başlıkta
`t = 0.30 s sim · tick 8` duruyor. Bunu göster: *"burada eksik veri yok, henüz
simüle edilmiş zaman yok."*

---

## 3. Hangi özelliklerden bahsetmeli

### Asıl iddia (bunu net söyle)

> **Doğal dille anlatılan bir senaryoyu, gerçek E2AP üzerinden koşan gerçek bir
> ns-3 RAN'a çeviriyoruz ve müşterinin kendi xApp'i ona canlı bağlanıyor.**

Üç parça ayrı ayrı var olan şeyler; birlikte olması bizim yaptığımız.

### Güçlü olduğumuz yerler

**1. AI senaryo tasarımı — asıl farkımız.**
169 şema alanı, **157'si ölçülerek** motora ulaştığı doğrulanmış, **0 tanesi ölü**.
Motorun açtığı 595 attribute'un sürmediğimiz 459'u da sınıflandırılmış, gerekçeli.
"Yapılmadı" değil, "şu sebeple reddedildi".

**2. Gerçek E2AP, taklit değil.**
ns-3.42 mmWave + ns-O-RAN + FlexRIC, SCTP üstünde gerçek E2AP. Şüphe ederlerse
upstream'in kendi panosunu açıp kendi senaryosunu koşturuyoruz
([ns3-kanit.md](ns3-kanit.md)).

**3. Müşterinin kendi xApp'i.**
`/ws/e2-real` üzerinden düz JSON — FlexRIC SDK'sı gerekmiyor. Arayüzden hazır
konnektör indiriliyor. Tarayıcı da bir xApp; gösterdiğimiz şey onların
kullanacağı şeyin aynısı.

**4. Gerçek kontrol, gerçek etkiyle.**
E2SM-RC ile hücre uyutma/uyandırma, handover yönlendirme, **QCI başına MAC PRB
kotası**, arıza enjeksiyonu. Hepsi koşan RAN'a iniyor ve ölçümde görünüyor.

**5. Enerji gerçekten ölçülüyor.**
RU uyku durumları (micro/light/deep), hücre başına Watt. Kapalı hücre 0 W'a
oturuyor, ağ genelinde ~%32 tasarruf ölçüldü.

**6. Ölçüm dürüstlüğü — bunu ayrı bir özellik olarak sat.**
- Koşu kendi **simüle ufkunu** raporluyor (`span_s`), sessizce geçmiyor
- Bir KPM karesinin **bağımsız gözlem olup olmadığı** işaretli (`kpm_fresh`) —
  ölçtük, kare sayısı örnek sayısını ~10 kat şişiriyor
- Çöken bir koşu artık `truncated` diyor, temiz "ok" demiyor
- `tools/verify.py` her seferinde **neyi kontrol ETMEDİĞİNİ** de yazıyor

Bu maddeler bir pilot ortakta satış cümlelerinden daha çok iş görür.

### Sessizce geçilecek şeyler (sorulursa söyle, önden açma)

- Analitik katman (hızlı, her topoloji) — gerçek motor varken konu dağıtır
- Kampanya/leaderboard — 7. adımda gerekirse
- Auth/K8s — altyapı sorusu gelirse

---

## 4. Gelecek planları

**Yakın (haftalar)**
- Beam cache: ölçüldü, **1.26× bedava** (her örneklenen değer değişmedi). İmaja
  alınacak, daha geniş doğrulamayla.
- Soak testi: en uzun doğrulanmış koşu 300 s. MTBF, hızdan daha önemli hâle
  geldi.
- `rngRun=3/4`'te deterministik bir flex-TTI çökmesi açık.

**Orta — gerçek zamana yaklaşma (asıl mühendislik hikâyesi)**
Ölçülmüş üç katman:
1. **Spectrum channel'ı kaldırıp SINR'ı önceden hesaplanmış REM'den beslemek:
   ~4.3×** (iki bağımsız yöntemle doğrulandı — 18.678 örneklik profilde
   `MultiModelSpectrumChannel::StartRx` %77, ve doğrudan stub koşusu)
2. **Hücre başına ayrı süreç, çekirdeklere dağıtım.** Ancak 1'den sonra meşru:
   ortak kanal kalmayınca girişim REM'den analitik gelir.
3. **Gerçek zaman tempolama** — hız kazancı değil, ürünü "canlı" yapan şey.

Dürüst aritmetik: bir UE'nin tam L2 yığını ~1 wall-saniye/sim-saniye yiyor.
12 çekirdek → **~12 UE'de gerçek zaman**. 59 UE değil.

**Uzak**
- K8s deploy, çoklu eşzamanlı kullanıcı
- 5G-LENA `nr` ikinci motor profili (ağaçta derli, E2'si kapalı)
- srsRAN+ZMQ alternatif backend
- `ai_interactions`'dan few-shot ile üretici kalitesi

---

## 5. Gelecek sorular ve cevapları

### "Gerçekten ns-3 mi, yoksa istatistiksel bir model mi?"

Upstream ns-O-RAN'ın **kendi panosunu** aç, **kendi senaryosunu** koştur.
`git remote -v`, `git log -1`, `cat VERSION` → herkese açık repo, isimli commit,
3.42. Yamalarımız `git status`'ta sayılı: 32 dosya, +3060/-217.
Detay: [ns3-kanit.md](ns3-kanit.md).

### "Neden gerçek zamandan yavaş?"

Ölçülmüş, saklanacak bir şey değil:

| Config | gerçek zamandan |
|---|---|
| 1 hücre / 1 UE | 2.5× yavaş |
| 3 hücre / 3 UE | 8.3× |
| 3 hücre / 6 UE | 24.7× |
| 3 hücre / 12 UE | 51× |

**En iyi cevap bizim sayımız değil:** ns-O-RAN'ın kendi referans panosu da
**15.2× yavaş** (ölçtük). Bu bizim eksiğimiz değil, tam yığın bir ns-3 RAN'ın
maliyeti. Nasıl aşacağımız 4. bölümde.

### "Kaç kullanıcıyı kaldırır?"

150 UE / 16 hücre ölçüldü ve temiz koştu. Sınır UE sayısı değil, **UE başına
maliyet** — UE'yi 4'e katlamak, hücreyi 12'ye katlamaktan pahalı. Hücre neredeyse
bedava.

### "Kaç simüle saniye görüyoruz?"

Dürüst cevap: varsayılan pencerede **1–2 saniye**. `settings.kpm_collect_s` ile
doğrusal olarak alınıyor — 3 hücre/6 UE için 120 s pencere 4.85 saniye veriyor.
Bu yüzden batch katmanı bir **zaman evrimi testi değil, konfigürasyon başına
PHY/MAC doğrulayıcısı**. Zaman evrimi isteyen canlı oturumu kullanıyor.

### "Sonuçlarınız VIAVI/Keysight ile uyuşuyor mu?"

48 hazır Tera5G koşusuyla karşılaştırdık: **ortalama SINR 1.7 dB içinde
uyuşuyor**. Bizim dağılımımız 10 kat geniş, çünkü onlar hızlı sönümlemeyi (fast
fading) kaldırmış — biz kaldırmadık. Yani fark bir hata değil, bir tercih.

### "Gecikme (latency) rakamlarınız?"

**Bunu biz kullanmıyoruz ve nedenini söyleyelim:** `DRB.PdcpSduDelayDl` aynı
config ve aynı seed'le altı koşuda **14 kat** oynuyor (41–574 ms), throughput ise
1.1 kat. 4-5 örnek ve ilki bağlanma anının tamponunu taşıyor. Throughput, PRB ve
enerjiye bakıyoruz.

### "Deployment? Bulut?"

Bugün: WSL üstünde iki distro + Docker, tek makine. Postgres 16 (:5433, Alembic),
JWT+bcrypt auth, admin konsolu. K8s yol haritasında. Bir koşu = bir çekirdek
(tek iş parçacıklı ns-3), 6 eşzamanlı koşu 5.8× verim verdi — yani bulut
ölçeklenmesi çekirdek sayısıyla doğrusal.

### "Hangi LLM? Veri nereye gidiyor?"

OpenRouter üzerinden Claude Opus 5 (config üretimi) ve Haiku 4.5 (metin), ikisi
de env ile değiştirilebilir. **Yerel model ile çalıştırma** yol haritasında —
veri hassasiyeti olan bir pilotta ilk yapılacak şey bu olur.

### "Neyi yapamıyorsunuz?"

Bu soruyu bekle ve hazır cevapla — pilot ortakta en değerli an bu:

- Gerçek zamanlı değil (yukarıda rakamlar ve plan var)
- Hücre/bant başına ayrı handover eşiği **imkânsız**, eksik değil: yukarı akışta
  tek bir LTE anchor RRC'si bütün handover'lara karar veriyor
- Gecikme metriği gürültü
- `rngRun=3/4`'te deterministik bir çökme açık
- Saatlerce koşu için soak testi henüz yok
- Binaların eklenmesi bütün ağı istatistikselden geometrik LOS/NLOS'a geçiriyor —
  bilinçli kullanılması gereken bir anahtar

---

## 6. Demoyu bozabilecek üç şey

1. **`nsg_demo_up.bat` penceresini kapatmak.** WSL boştaki distroyu askıya alır,
   backend/frontend ölür, "Failed to fetch" görürsün. Küçült, kapatma.
2. **Motor ve upstream GUI'yi aynı anda koşturmak.** CPU'yu paylaşırlar.
3. **Canlı oturumu yarıda bırakıp yenisini başlatmak.** Motor tek senaryo koşar.
   Takılırsa: `wsl -d Ubuntu-2004-nsoran -u root docker restart nsg-engine`
