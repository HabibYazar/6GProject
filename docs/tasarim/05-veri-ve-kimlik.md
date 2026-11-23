# 5. Veri, kimlik ve ayarlar

## Veritabanı

PostgreSQL 16. Şema `nsg_ai/models.py`'de, göçler `alembic/versions/` altında
(6 sürüm: `5cfc63c96c74` ilk şema → … → `e5f6a7b8c9d0` ai_interactions). Backend
imajı açılırken `alembic upgrade head` çalıştırır.

| Tablo | Sütunlar (`*` = birincil anahtar) | Ne tutar |
|---|---|---|
| `users` | id\*, username, password_hash, role, disabled, created_at, last_login_at | Hesaplar. `role`: `admin` / `user`. |
| `scenarios` | id\*, name, description, config, source, changed_fields, created_at, owner_id | Kayıtlı senaryo anlık görüntüleri |
| `runs` | id\*, scenario_id, config, requested, status, kind, error, n_frames, summary, status_lines, started_at, finished_at, owner_id | Her koşunun meta verisi ve özeti. `kind`: `live` / `batch`. |
| `run_frames` | run_id\*, tick\*, payload | Koşunun KPM kareleri (JSON). En büyük tablo. |
| `campaigns` | id\*, name, description, status, verdict, base_seed, thresholds, planned, stop_requested, created_at, finished_at, owner_id | Kampanya tanımı ve hükmü |
| `campaign_runs` | id\*, campaign_id, position, label, item_label, repeat, seed, config, run_id, status, verdict, violations, summary, error, started_at, finished_at | Kampanyanın her koşusu ve PASS/FAIL gerekçesi |
| `ai_interactions` | id\*, created_at, owner_id, model, prompt, ai_config, final_config, config_diff, outcome, outcome_source, run_id | Üreticinin kaydı: ne istendi, ne üretildi, ne tutuldu, hangi koşuya dönüştü |

**Yeni bir göç eklemek:** `models.py`'yi değiştirin, sonra backend imajı içinde
`alembic revision --autogenerate -m "<ne değişti>"` çalıştırın. Üretilen dosyayı
elle gözden geçirin; autogenerate her şeyi yakalamaz. Nasıl çalıştırılacağı:
[08](08-gelistirme.md#veritabanı-göçü).

## Kimlik ve sahiplik

- **Yerel JWT** (`nsg_ai/auth.py`): parolalar özetlenir, token HS256 ile
  `NSG_AUTH_SECRET` kullanılarak imzalanır. E-posta yok, bu yüzden parola sıfırlamanın
  tek yolu bir admin'in yeni parola atamasıdır.
- **İlk kullanıcı kuralı:** boş `users` tablosuna ilk kaydolan kimlik bilgisi
  gerektirmeden **admin** olur. Sonrasında yalnız admin kullanıcı ekleyebilir.
- **Sahiplik** `owner_id` iledir; kullanıcı adı yalnızca etikettir, yeniden
  adlandırmak veriyi koparmaz.
- **Görünürlük kuralı** (`main._may_see`):
  - Kendi kaydınız her zaman görünür.
  - Sahipsiz kayıtlar (kimlik doğrulama öncesinden kalanlar) yalnız admin'e görünür.
  - Başkasının kaydını admin yalnız **isterse** görür (`?all=true`). Varsayılan
    liste admin'in kendi işidir.
  - Normal kullanıcı `?all=true` ile yetki yükseltemez; başkasının kaydı 404 döner.
- **Kullanıcı yönetimi:** arayüzde `/admin/users`. Ya da backend kapalıyken bile
  çalışan `tools/nsg_user.py` (`list`, `create`, `passwd`, `rename`, `role`,
  `disable`, `enable`, `delete`). Parola argümandan değil, istemden alınır. Tek
  admin kilitlenirse geri dönüş yolu budur.
- **Uç nokta seviyesinde koruma:** [04](04-api.md#kimlik-doğrulama).

## Ortam değişkenleri

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `NSG_DATABASE_URL` | — (zorunlu) | `postgresql+psycopg://kullanıcı:parola@host:port/db`. Yoksa backend açılmaz. |
| `NSG_AUTH_SECRET` | boş | JWT imza anahtarı. Dağıtımda rastgele üretilir; değişirse mevcut token'lar geçersizleşir. |
| `NSG_AUTH_REQUIRED` | `0` | `1` ise giriş zorunlu. Dağıtım paketi `1` kullanır. |
| `NSG_AUTH_TOKEN_TTL_HOURS` | `12` | Token ömrü |
| `OPENROUTER_API_KEY` | — (zorunlu) | LLM erişimi. **Tanımlı değilse backend açılmaz** (`llm_agent` içe aktarılırken hata verir). |
| `NSG_CONFIG_MODEL` | `anthropic/claude-opus-5` | Konfigürasyon üreten model (OpenRouter adı) |
| `NSG_TEXT_MODEL` | `anthropic/claude-haiku-4.5` | Metin / özet modeli |
| `NSG_NSORAN_URL` | `http://localhost:8090` | Motorun toplu doğrulama API'si (compose'da `http://engine:8090`) |
| `NSG_NSORAN_LIVE` | `127.0.0.1:8091` | Motorun canlı köprüsü (compose'da `engine:8091`) |
| `NSG_ENGINE_POOL` | boş → `NSG_NSORAN_LIVE` | Kampanya için canlı köprü adresleri, virgülle. Her adres aynı anda **bir** koşu alır. |
| `NSG_ENGINE_COOLDOWN_S` | `20` | Havuzda bir motor örneğinin iki koşu arası dinlenmesi |
| `NSG_CORS_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | İzin verilen tarayıcı kökenleri, virgülle |
| `NSG_API`, `NSG_QA_USER`, `NSG_QA_PASS`, `NSG_LLM_TESTS`, `NSG_LLM_MODEL` | — | Yalnız test ve araçlar kullanır |
| `NEXT_PUBLIC_API_URL` (arayüz) | `http://localhost:8000` | Arayüzün backend adresi. **Derleme anında** gömülür, çalışma anında değiştirilemez. |

Dağıtım paketindeki karşılıkları: `.env` (DB parolası, auth anahtarı, zorunluluk,
isteğe bağlı `NSG_ENGINE_IMAGE` / `NSG_ENGINE_POOL`) ve `nsg_ai.env`
(`OPENROUTER_API_KEY`). İkisi de `install.sh` tarafından yazılır. Bkz.
[09](09-kurulum-ve-isletim.md).

## Yedekleme ve geri yükleme

Paket klasöründe:

```bash
sudo docker compose exec -T db pg_dump -U nsg -d nsg -Fc > yedek-$(date +%F).dump
```

```bash
sudo docker compose exec -T db pg_restore -U nsg -d nsg --clean --if-exists --no-owner --no-privileges < yedek.dump
```

Veri `nsg_nsg-db` adlı Docker volume'unda durur. `docker compose down` onu
**korur**; `docker compose down -v` **siler**.
