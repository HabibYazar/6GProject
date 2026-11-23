# 9. Kurulum ve işletim

## Dağıtım paketi

Tek bir Ubuntu makinesine, **internet gerekmeden** kurulur.

| Dosya | İçerik |
|---|---|
| `images/nsg-engine-1.32-rxtrace.tar.gz` | Motor (çalışma zamanı) |
| `images/nsg-backend-*.tar.gz`, `images/nsg-frontend-*.tar.gz` | Backend ve arayüz |
| `images/postgres-16.tar.gz` | Veritabanı |
| `images/nsg-engine-base-20.04.tar.gz` | Motor image'ının tabanı (motoru ağsız yeniden derlemek için) |
| `images/nsg-engine-dev-rootfs.tar.gz` | Motor geliştirme ortamı (≈3.8 GB sıkıştırılmış, ≈16 GB açılmış) |
| `db/nsg.dump` | Başlangıç veritabanı: kullanıcılar, koşular, kampanyalar, senaryolar |
| `src/nsg_ai_platform.bundle` | Git reposu, tüm geçmişiyle |
| `docker-compose.yml`, `install.sh`, `dev.sh`, `KURULUM.txt`, `SHA256SUMS` | Kurulum ve geliştirme |

**Gereksinimler:** Ubuntu, x86_64, Docker + Docker Compose v2. Çalıştırmak için
en az 8 GB, geliştirme ortamıyla birlikte ≈30 GB boş disk. Bir motor koşusu
bir çekirdek kullanır; çekirdek sayısı eşzamanlı kampanya koşularını belirler.

## Kurulum

```bash
tar -xf nsg_bundle.tar && cd nsg_bundle
sudo ./install.sh
```

`install.sh` şunları yapar:

1. Paket bütünlüğünü (sha256), mimariyi, Docker'ı ve diski kontrol eder.
2. Image'ları yükler.
3. `.env` dosyasını rastgele veritabanı parolası ve oturum anahtarıyla, `nsg_ai.env`
   dosyasını OpenRouter anahtarıyla yazar.
4. Veritabanı boşsa yedeği yükler.
5. Servisleri başlatır ve hem compose ağı içinden hem makinenin kendisinden
   (`localhost:8000`, `localhost:3000`) doğrular.

İki kez çalıştırmak güvenlidir.

**OpenRouter anahtarı zorunludur:** backend onsuz açılmaz. `sudo
OPENROUTER_API_KEY=... ./install.sh` ile ya da sorulduğunda verilir. İnternet
yoksa anahtar geçersiz de olabilir; yalnız AI sohbeti çalışmaz.

## Servisler

| Servis | Adres | Not |
|---|---|---|
| Arayüz | http://localhost:3000 | |
| Backend | http://localhost:8000 (Swagger: `/docs`) | |
| Postgres | `127.0.0.1:5433` | Kullanıcı `nsg`, parola `.env`'de |
| Motor | yalnız compose ağında: `engine:8090`, `engine:8091` | Bilerek dışarıya yayınlanmaz |

Her şey `127.0.0.1`'e bağlıdır: platforma yalnız bu makinenin kendisinden
erişilir. Başka bir makineden açmak için:

1. Arayüzü doğru backend adresiyle yeniden derleyin (`NEXT_PUBLIC_API_URL`
   derleme anında gömülür).
2. Compose'da portları açın.
3. `NSG_CORS_ORIGINS`'e yeni kökeni ekleyin.

Bunu yapmadan önce motor API'sinin ve `/ric/validate-nsoran`'ın kimlik doğrulaması
olmadığını hatırlayın ([10](10-bilinen-sinirlar.md#güvenlik)).

Kullanıcılar: `admin`, `demo1`, `demo2`, `isolation_a`, `isolation_b`; parolalar
önceki kurulumdakilerle aynı. Bilinmiyorsa:

```bash
sudo docker compose exec backend python /app/tools/nsg_user.py passwd admin
```

## `.env` ayarları (paket klasöründe)

| Anahtar | Anlamı |
|---|---|
| `NSG_DB_PASSWORD`, `NSG_AUTH_SECRET` | `install.sh` üretir. Değiştirmeyin; DB parolası volume'daki veritabanında da kayıtlı. |
| `NSG_AUTH_REQUIRED` | `1` = giriş zorunlu |
| `NSG_ENGINE_IMAGE` | Motor image'ı (varsayılan `nsg-engine:1.32-rxtrace`) |
| `NSG_BACKEND_IMAGE`, `NSG_FRONTEND_IMAGE` | Backend / arayüz image'ı (varsayılan pakettekiler) |
| `NSG_ENGINE_POOL` | Kampanya için motor adresleri |

Değişiklikten sonra: `sudo docker compose up -d`.

## Kampanya için ek motor

Bir motor örneği aynı anda tek koşu yapar. Kampanyaları paralelleştirmek için:

```bash
sudo docker compose --profile pool up -d
```

`.env`'e `NSG_ENGINE_POOL=engine:8091,engine2:8091,engine3:8091` ekleyin ve
`sudo docker compose up -d backend`. Ölçülen kazanç, geliştirme makinesinde
üç örnekle 2.4× (gerçekçi yük) ile 3.8× (hafif yük) arası. Örnek sayısını
**çekirdek sayısına** göre seçin, belleğe göre değil.

## Günlük işletim

```bash
sudo docker compose ps                    # durum
sudo docker compose logs -f backend       # günlük (engine, frontend, db de olur)
sudo docker compose stop                  # durdur (veriler kalır)
sudo docker compose up -d                 # başlat
```

Servisler makine açılınca kendiliğinden başlar (`restart: unless-stopped`).

**Yedek:** [05](05-veri-ve-kimlik.md#yedekleme-ve-geri-yükleme). Veri `nsg_nsg-db`
volume'undadır. `docker compose down -v` onu **siler**.

## Sorun giderme

| Belirti | İlk bakılacak |
|---|---|
| `install.sh`: "tarayıcı yolu … açılmıyor" | Konteynerler ayakta ama makineden yayınlanan porta ulaşılamıyor. `sudo docker compose ps` ile port eşlemesine bakın. Güvenlik duvarı ya da Docker ağ ayarı olabilir. |
| Arayüz "Failed to fetch" | Backend ayakta mı (`compose ps`)? Arayüz `http://localhost:8000`'e gidiyor: tarayıcının açıldığı makinede backend var mı? |
| Backend sürekli yeniden başlıyor | `compose logs backend`. En sık neden: `OPENROUTER_API_KEY` yok ya da veritabanı adresi yanlış. |
| AI sohbet cevap vermiyor | İnternet yok ya da anahtar geçersiz. Diğer özellikler etkilenmez. |
| Koşu `no_data` / boş | `sudo docker compose exec engine tail -5 /tmp/scn0.log`. Son satır ns-3'ün tam komut satırıdır. |
| Koşu `busy` | Motor örneği başka bir koşuda. Tek örnek aynı anda tek koşu yapar; havuzu büyütün. |
| Koşu `truncated` | ns-3 yolda öldü, eldeki KPM gerçek. Bilinen çökmeler: [10](10-bilinen-sinirlar.md#motor-slow-tier). |
| Normal kullanıcı eski koşuları görmüyor | 2026-09-24 öncesi canlı koşular sahipsiz kaydedildi; yalnız admin görür ([10](10-bilinen-sinirlar.md#veri)). |
