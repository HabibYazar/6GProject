# 8. Geliştirme

## Ortam

Bu makinede internet olmadığı varsayılır. Her şey dağıtım paketindeki
image'lardan açılan container'larda çalışır; makinenin kendi Python, Node ya da
git sürümü önemsizdir. Araç: paket klasöründeki **`dev.sh`**.

```bash
sudo ./install.sh          # bir kez: platformu kurar (bkz. 09)
./dev.sh setup             # bir kez: repo + motor geliştirme ortamı
```

`setup` şunları yapar: motor derleme ortamını (`nsg-engine-dev:20.04`, ≈16 GB)
içe aktarır, repoyu git bundle'ından `~/nsg_ai_platform`'a klonlar (dal
`feature/full-sim-pipeline`) ve kalıcı `nsg-engine-dev` container'ını oluşturur.

| Komut | Ne yapar |
|---|---|
| `./dev.sh backend` | Paketteki backend'i durdurur, repodaki kodu `:8000`'de **otomatik yeniden yüklemeyle** çalıştırır (önce `alembic upgrade head`). Ctrl+C ile çıkınca paketteki backend geri gelir. |
| `./dev.sh frontend` | Aynısı arayüz için: repodaki `ui/`, `next dev`, `:3000`. `node_modules` image'dan gelir. |
| `./dev.sh test` | `tools/verify.py --fast` (backend image'ında) + `tsc --noEmit` (arayüz image'ında) |
| `./dev.sh shell` | Backend'in Python'u ve veritabanı erişimiyle bir kabuk; repo `/app`'te |
| `./dev.sh images <etiket>` | Repodaki koddan yeni backend + arayüz image'ı, **ağsız** |
| `./dev.sh engine` | Motor derleme ortamında `builder` kabuğu, ns-3 dizininde |
| `./dev.sh engine-image <etiket>` | Motor derleme ortamından yeni motor image'ı, **ağsız** |
| `./dev.sh git <argümanlar>` | Makinede git yoksa, motor ortamındaki git ile |

İnternetli bir makinede alternatif: Python 3.12 venv (`pip install -r requirements.txt`),
Node 22 (`cd ui && npm ci --legacy-peer-deps`), bir Postgres. Sonra
`cd nsg_ai && uvicorn main:app` (backend **`nsg_ai/` içinden** çalıştırılmalı,
modüller üst seviye import edilir) ve `cd ui && npm run dev`.

## Git

Repo git bundle'ından klonlandığı için `origin` o dosyayı gösterir. Kendi
sunucunuzu ekleyin:

```bash
./dev.sh git remote set-url origin <sizin-repo-adresiniz>
```

Tüm geçmiş (`main` ve `feature/full-sim-pipeline`) repoda. `git log` bu projenin
en iyi tarihçesidir: commit mesajları problemi, nasıl bulunduğunu ve neyle
doğrulandığını anlatır. Bu alışkanlığı sürdürmenizi öneririz.

## Test

```bash
./dev.sh test
```

`verify.py` üç kademelidir: `--fast` (saniyeler, motor ve ağ yok), varsayılan
(+ motor bağlantıları, WSL düzeni ister), `--all` (+ gerçek koşular). Çalıştıramadığı
kontrolü **nedeniyle birlikte `SKIP`** yazar. Bu makinede motor kademesi WSL'e özgü
olduğundan atlanır; motor değişikliklerini ürün yolundan test edin (aşağıda).

Uçtan uca testler çalışan bir backend'e karşı koşar. `./dev.sh shell` içinden:

```bash
NSG_API=http://backend:8000 python nsg_ai/test_isolation.py
NSG_API=http://backend:8000 python nsg_ai/test_ws_auth.py
```

`test_isolation.py` admin'e `admin` / `admin12345` ile girer. Veritabanındaki
admin parolası farklıysa geçici olarak değiştirin (`tools/nsg_user.py passwd admin`)
ya da testi atlar.

## Yeni bir konfigürasyon alanı eklemek

Bir alanın gerçekten bir şeyi değiştirmesi için zincirin her halkası gerekir.
Aksi halde "arayüz kabul ediyor, motor yok sayıyor" hatası oluşur; bu kod tabanı
bu sınıfı bir kez tamamen temizledi.

1. **Şema:** `nsg_ai/schema.py`. Varsayılan değer ve aralık, açıklama.
2. **Motor parametresi:** `main.nsoran_params()` alanı motorun parametre sözlüğüne
   çevirir.
3. **Motora taşıma:** `engines/nsoran/engine_service.py` **ve**
   `engines/nsoran/live_bridge_server.py` içindeki `_PARAM_ENV`. İkisi de; biri
   eksik kalırsa toplu ve canlı yol ayrışır. `tools/test_path_parity.py` bunu yakalar.
4. **Script → ns-3:** `run_scenario.sh` / `run_live_session.sh` ortam değişkenini ns-3
   argümanına çevirir; senaryo (`scratch/scenario-zero-with_parallel_loging.cc`)
   onu bir `GlobalValue` ya da attribute olarak okur.
5. **Fast tier:** `tier_b.py` / `window_model.py` alanı okur **ya da** alan
   `schema.TIER_SCOPED`'a **nedeniyle** eklenir.
6. **Denetim:** `python tools/param_audit.py --cached --check` alanın motora ulaştığını
   ölçer (alanı değiştirip parametre farkına bakar).
7. **AI:** `nsg_ai/test_prompt_contract.py` üreticinin alanı ifade edebildiğini denetler.
8. **Arayüz:** editör `/schema`'dan beslenir, ayrıca iş gerekmez.

## Veritabanı göçü

```bash
./dev.sh shell
# kabukta:
alembic revision --autogenerate -m "ne değişti"
# alembic/versions/ altındaki yeni dosyayı gözden geçirin, sonra:
alembic upgrade head
```

## Değişikliği dağıtıma almak (backend / arayüz)

```bash
./dev.sh images 2026-10-01
```

Repodaki kodu, paketteki image'ların (tüm bağımlılıklar içlerinde) üstüne koyarak
`nsg-backend:2026-10-01` ve `nsg-frontend:2026-10-01` üretir. Sonra paket
klasöründeki `.env` dosyasına:

```
NSG_BACKEND_IMAGE=nsg-backend:2026-10-01
NSG_FRONTEND_IMAGE=nsg-frontend:2026-10-01
```

ve `sudo docker compose up -d backend frontend`.

**Bağımlılık değişikliği internet ister.** `requirements.txt` ya da
`ui/package-lock.json` değiştiyse `images` açıklamayla durur. İnternetli bir
makinede repodaki `docker/backend.Dockerfile` / `docker/frontend.Dockerfile` ile
derleyin, `docker save` ile taşıyın.

> Arayüz fontları repodan servis edilir (`ui/public/fonts/`). Önceden
> `next/font/google` kullanıyordu ve `next build` internetsiz makinede başarısız
> oluyordu. Başka bir derleme anı indirmesi eklemeyin.

## Motor geliştirme

Motor derleme ortamı Ubuntu 20.04'ün tamamıdır: derleyiciler, bağımlılıklar,
ns-3 ve FlexRIC kaynak ve derleme ağaçları. `nsg-engine-dev` container'ında
yaşar; `/home/builder` ve `/root` isimli volume'larda (`nsg-dev-builder`,
`nsg-dev-root`) tutulur, container silinse bile emek kaybolmaz. Repo
container'da `/repo` olarak görünür.

**Bir C++ değişikliğinin ürüne ulaşması:**

```bash
./dev.sh engine                      # builder kabuğu, ns-3 dizininde
# düzenleyin: src/mmwave/..., src/lte/..., scratch/scenario-zero-with_parallel_loging.cc
./ns3 build                          # artımlı; src/ değişikliği tam kütüphane derlemesi ister
exit
./dev.sh engine-image nsg-engine:1.33-degisikligim
```

Sonra paket klasöründeki `.env`'e `NSG_ENGINE_IMAGE=nsg-engine:1.33-degisikligim`
yazın ve `sudo docker compose up -d engine`. Bir koşuyla (arayüzden ya da
`POST /sim/run`) doğrulayın; koşu boş dönerse motor container'ında `/tmp/scn0.log`'un
son satırına bakın.

**Kaydetmek:** değişiklik repoya iki biçimde girer (bkz.
`engines/nsoran/tree-snapshot/README.md`):

1. `engines/nsoran/tree-snapshot/*.diff` anlık görüntülerini yeniden üretin.
   Upstream'e karşı **tam** fark budur.
2. Konvansiyon olarak, değişikliği ve **nedenini** başlığında anlatan bir
   `engines/nsoran/<ad>.patch` yazın. Mevcut 39 yama örnektir.

**Bilinmesi gerekenler:**

- `default` derleme profilinde kalın. `-d optimized`, platformun ayrıştırdığı her
  `[NSG-*]` günlük satırını sessizce siler.
- ns-3 root olarak çalışmaz: `builder` kullanın (`./dev.sh engine` öyle açar).
- Senaryo binary'sini **doğrudan** çalıştırmak trafik kurmaz ve ürünün çöktüğü
  noktaları "geçer" gösterir. Doğrulamayı ürün yolundan (`:8090` / `:8091`) yapın.
- Motor kodu üç yerde yaşar (repo → `/root/nsoran` → image). `engine-image` repodaki
  script'leri `/root/nsoran`'a kopyalayıp öyle derler. Derleme ortamında elle
  değiştirdiğiniz bir script'i repoya geri yazmayı unutmayın.
- Motor image'ı `--network none` ile derlenir; taban (`nsg-engine-base:20.04`)
  pakette hazırdır. Taban paketlerini değiştirmek internet ister
  (`engines/nsoran/Dockerfile.base`).

## WSL'e özgü araçlar

Proje bir Windows + WSL makinesinde geliştirildi. Bu araçlar o düzeni varsayar,
bu makinede **çalışmaz**:

| Araç | Ne yapıyordu | Burada yerine |
|---|---|---|
| `tools/preflight.py` | Tüm bağlantıları iki WSL dağıtımı arasında kanıtlar | `sudo docker compose ps` + `install.sh`'ın son kontrolleri |
| `tools/engine_sync.py` | repo ↔ `/root/nsoran` ↔ image kaymasını ölçer | `./dev.sh engine-image` repoyu kopyalayıp derler; kayma kaynağı ortadan kalkar |
| `tools/nsg_demo_up.bat`, `tools/demo_app_up.sh`, `engines/nsoran/demo_engine_up.sh` | Windows'tan demo açma | `sudo docker compose up -d` |
| `tools/engine_pool_up.sh` | Ek motor örnekleri | compose `pool` profili (bkz. [09](09-kurulum-ve-isletim.md#kampanya-için-ek-motor)) |
| `tools/nightly_fidelity.sh`, `fidelity_boot_code.txt` | Windows zamanlayıcıyla gece ölçümü | cron + compose içinde ilgili araç |
| `tools/nsoran_gui_up.sh` | Upstream'in kendi paneli (ns-3 kanıtı) | Motor geliştirme ortamında elle |

Kalan araçların çoğu backend'e HTTP ile konuşur; `./dev.sh shell` içinden
`NSG_API=http://backend:8000` ile çalıştırılabilir. Her birinin amacı:
[`tools/README.md`](../../tools/README.md).
