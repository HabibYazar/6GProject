# NSG AI Platform — Tasarım ve Yapı Dokümanları

Bu klasör, platformu hiç görmemiş bir ekibin **sistemi anlaması, çalıştırması,
geliştirmesi ve motoru yeniden derlemesi** için yazıldı. Önceki geliştiriciye
soru sormadan ilerleyebilmeniz hedeflendi. Bir şey burada yoksa kaynak kodda
cevap vardır: modüllerin başındaki docstring'ler bu kod tabanında uzun ve
özenlidir, çoğu "neden" sorusunun cevabı oradadır.

> **Anlık görüntü:** 2026-09-24 · dal `feature/full-sim-pipeline` · motor imajı
> `nsg-engine:1.32-rxtrace` · veritabanı göçü `e5f6a7b8c9d0`.

## Okuma sırası

| # | Doküman | Ne anlatır | Kim okumalı |
|---|---|---|---|
| 1 | [01-genel-bakis.md](01-genel-bakis.md) | Platform ne yapar, iki simülasyon katmanı, kavram sözlüğü | Herkes |
| 2 | [02-mimari.md](02-mimari.md) | Bileşenler, portlar, veri akışları (diyagramlarla) | Herkes |
| 3 | [03-kod-haritasi.md](03-kod-haritasi.md) | Hangi dosya ne iş yapar | Geliştirici |
| 4 | [04-api.md](04-api.md) | Tüm HTTP / WebSocket uç noktaları | Backend, arayüz, xApp yazan |
| 5 | [05-veri-ve-kimlik.md](05-veri-ve-kimlik.md) | Veritabanı, kullanıcılar, sahiplik, ortam değişkenleri | Backend, işletim |
| 6 | [06-motor-slow-tier.md](06-motor-slow-tier.md) | Gerçek ns-O-RAN motoru (ns-3 + FlexRIC) | Motor geliştirici |
| 7 | [07-fast-tier.md](07-fast-tier.md) | Hızlı katman: kapalı-form pencere modeli | Model / backend geliştirici |
| 8 | [08-gelistirme.md](08-gelistirme.md) | Günlük geliştirme, test, motor yeniden derleme | Geliştirici |
| 9 | [09-kurulum-ve-isletim.md](09-kurulum-ve-isletim.md) | Kurulum, servisler, yedekleme, güncelleme | İşletim |
| 10 | [10-bilinen-sinirlar.md](10-bilinen-sinirlar.md) | Bilinen hatalar, açık işler, tuzaklar | Herkes — ölçüme güvenmeden önce |

## Repodaki diğer dokümanlar — ne kadar güncel?

Bunlar silinmedi, ama yazıldıkları tarihteki durumu anlatırlar. Çelişki olursa
**bu klasör ve kaynak kod** geçerlidir.

| Dosya | Tarih | Not |
|---|---|---|
| [`../../README.md`](../../README.md) | 2026-09-16 | İngilizce giriş. Kurulum komutları geliştirme makinesinin **Windows + WSL** düzenine göre yazılmış. |
| [`../SISTEM.md`](../SISTEM.md) | 2026-09-18 | Türkçe sistem özeti. Motor etiketi (`1.25`) ve WSL komutları eskidi; fast tier'dan bahsetmez. |
| [`../engine-veri-ve-config-durumu.md`](../engine-veri-ve-config-durumu.md) | 2026-07-29 | Kendi başlığında da yazdığı gibi bir **ölçüm raporu**, güncel durum tablosu değil. |
| [`../ns3-kanit.md`](../ns3-kanit.md) | — | Bir şüpheciye motorun gerçekten ns-3 olduğunu kanıtlama yöntemi. |
| [`../demo-kitapcigi.md`](../demo-kitapcigi.md) | — | Pilot / ortak görüşmesi için demo akışı. |
| [`../../tools/README.md`](../../tools/README.md) | güncel | **Araç indeksi** — her aracın hangi soruyu cevapladığı. Bazı araçlar WSL'e özgüdür, bkz. [08](08-gelistirme.md#wsle-özgü-araçlar). |
| [`../../engines/README.md`](../../engines/README.md), [`../../engines/nsoran/README.md`](../../engines/nsoran/README.md) | 2026-09 / 2026-07 | Motor haritası; etiket numaraları eskidi. |

## Bu kod tabanının üç alışkanlığı

Kodu okurken bunları bilmek zaman kazandırır:

1. **Ölç, iddia etme.** Bir sayı kodda sabitse yanında kaynağı yazar (motorun
   hangi C++ fonksiyonundan okunduğu, hangi araçla ölçüldüğü). Kaynaksız bir
   sabit eklemeyin.
2. **Atlanan kontrol geçmiş sayılmaz.** `tools/verify.py` çalıştıramadığı bir
   kontrolü nedeniyle birlikte `SKIP` olarak yazar. "12 geçti" dediğinde dördü hiç
   çalışmamış olamaz.
3. **Commit mesajı problemi anlatır.** Başlık neyin yanlış olduğunu söyler
   ("A normal user could not see their own run once it finished"), gövde nasıl
   bulunduğunu ve neyle doğrulandığını. `git log` bu projenin en iyi tarihçesidir.
