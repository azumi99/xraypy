# cv-service — Auto-capture X-ray Parcel + Dedup + AWB Pairing (Full Python)

FastAPI service yang menangkap video dari layar mesin X-ray (HDMI capture),
mendeteksi tiap paket pada strip bergulir, menghitung embedding visual
(DINOv2/ResNet), memutuskan **NEW / REVIEW / DUPLICATE**, lalu memasangkan
paket **NEW** ke antrean **AWB** secara berurutan (`idx`) — semuanya
full Python, tanpa NestJS.

## Alur kerja

1. Operator **insert AWB** dulu (`POST /awb` atau tab AWB di web UI) → masuk antrean WAITING.
2. Paket melewati X-ray; layar bergulir **ke kanan**, objek baru masuk dari kiri.
   `StripTracker` (phase correlation + profil kolom) mendeteksi objek yang
   **selesai masuk**. Objek yang sudah ada di layar saat sesi dimulai
   (**baseline**) tidak di-capture dan tidak diberi idx.
3. Crop kontur BGRA (latar transparan) → embedding di thread terpisah →
   pencarian tetangga di pgvector (model aktif saja) → keputusan:
   - `sim ≥ t_high` → **DUPLICATE**: tanpa idx, tanpa AWB, event `parcel.duplicate`.
   - `t_low ≤ sim < t_high` → **REVIEW**: ditahan untuk operator.
   - `sim < t_low` → **NEW**: `idx` berikutnya (transaksi + advisory lock per
     batch) **dan** pasangan AWB berikutnya (FIFO) dalam transaksi yang sama.
4. Overlay web menampilkan status `tracking | saved | duplicate | review`;
   `index` null untuk duplicate/review (idx tidak pernah berlubang).

## Instalasi Windows

Lihat [panduan instalasi Windows](INSTALL-WINDOWS.md) untuk setup Python, venv, frontend, dan backend.

## Instalasi (macOS, CPU)

```bash
brew install pgvector                 # ekstensi pgvector utk Postgres Homebrew
brew services start postgresql@18     # jika belum jalan
createdb object_cv

cd cv-service
/opt/homebrew/bin/python3.11 -m venv .venv      # torch belum dukung 3.14
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env                  # sesuaikan bila perlu

# unduh bobot model SEBELUM runtime (sekali saja)
.venv/bin/python tools/preload_models.py          # dinov2_small + yolo11n-seg
# .venv/bin/python tools/preload_models.py resnet18 resnet50   # alternatif
```

Jalankan:

```bash
.venv/bin/uvicorn app.main:app --port 8100
# buka http://127.0.0.1:8100  (tab Live / AWB / Review)
```

Migrasi schema `cv` (parcel_identity, parcel_embedding, review_audit, awb,
session + index HNSW) berjalan otomatis saat startup. Gagal DB/pgvector →
service menolak start dengan petunjuk perbaikan.

## Standalone CAS photo sync

**Legacy:** do not run this rsync job against the same destination as the
application photo-sync receiver. It can send an unfinished capture or overwrite
an already verified photo. Stop existing jobs before enabling the new receiver.

`tools/photo_sync.py` is a separate Python 3.11+ process, independent of the API and database. It sends only regular PNG/JPG/JPEG files under the locally owned `data/crops/cas/` tree, retaining `cas/` in the destination. Legacy root files and other tenants are excluded. Hidden entries, common temporary names and symlinks are skipped. No remote files are deleted. The first scan is immediate; reconciliation repeats 15 seconds after each attempt, with checksums and retries after failures. Rsync normally transfers into a temporary file and renames it into place; it is not invoked with `--inplace`.

Configure these environment variables outside source control (all local paths must be absolute, exist, and have no symlink components):

```sh
export PHOTO_SYNC_SOURCE=/absolute/path/to/cv-service/data/crops
export PHOTO_SYNC_USER=photo_sync
export PHOTO_SYNC_HOST=photos.example.com
export PHOTO_SYNC_PORT=22
export PHOTO_SYNC_DEST=/srv/cv-photos
export PHOTO_SYNC_KEY=/absolute/path/to/dedicated_private_key
export PHOTO_SYNC_KNOWN_HOSTS=/absolute/path/to/pinned_known_hosts
/usr/bin/python3 /absolute/path/to/cv-service/tools/photo_sync.py
```

Pin the verified server host key in the dedicated known-hosts file beforehand, and provision an unencrypted, dedicated SSH key restricted on the server to the intended rsync destination and operations. The remote account and destination must be independently secured: the client cannot verify remote directory symlinks or enforce server-side permissions. Destination is an absolute path restricted to simple ASCII path components, and must already exist. SSH uses strict host checking, no interactive authentication, and no user SSH configuration. `rsync` and `ssh` must be available on the service's PATH on the Mac, and compatible rsync must be available remotely. macOS openrsync is supported; test interoperability with the remote rsync before deployment.

Run as a separate launchd user agent with an explicit working directory, PATH, environment variables, and log destinations; launchd does not automatically load shell profiles. This repository does not install or configure launchd or the remote server. A per-source local lock prevents concurrent instances under the same user; failures log only generic messages and retry. Monitor the process and logs externally.

This is one-way replication, not a backup or conflict resolution: a changed local file replaces the remote file at the same path (last successful writer wins), including one previously changed remotely. Deleting locally never deletes remotely. Only one machine should own a given path. Files must be finalized atomically by the producer; a file changing during a scan/transfer can be retried on the next pass. The source tree must not be writable by untrusted users: path checks and rsync's no-links option are not a defense against malicious concurrent directory replacement. Remote compromise, remote symlinks, network outages, disk exhaustion, and incompatible rsync implementations can prevent or redirect writes; no delivery guarantee or remote integrity audit is provided.

## Sumber video

| `CV_CAPTURE__SOURCE_KIND` | Arti |
|---|---|
| `synthetic` (default) | Simulator layar HiTraX 640×360 — untuk dev/demo tanpa hardware |
| `device` | HDMI capture card; `CV_CAPTURE__SOURCE=0` (index cv2) |
| `file` | File video rekaman layar scanner; `CV_CAPTURE__SOURCE=/path.mp4` (diloop) |

## Menyetel ke mesin asli (penting!)

Nilai default disetel dari contoh screenshot HiTraX 640×360
(`white_thr=200` karena latar layar ~222 abu-abu, ROI `x=2,y=0,636×308`
untuk membuang bar UI bawah). Untuk feed HDMI Anda sendiri:

1. Rekam 10–30 detik layar scanner saat 2–3 paket lewat (`CV_CAPTURE__SOURCE_KIND=file`).
2. Sesuaikan `CV_STRIP__ROI_X/Y/W/H` agar hanya area citra yang masuk
   (buang UI magenta/teks — warna magenta murni sudah difilter, tapi ROI
   lebih aman).
3. Verifikasi `CV_STRIP__WHITE_THR` (ambang "objek vs latar") dan
   `CV_STRIP__MIN_GAP` (lebar celah antar paket, px).
4. `CV_STRIP__MAX_DX` / `CV_STRIP__MIN_RESPONSE` menjaga saat pergeseran
   tak terbaca — objek baru tidak didaftar pada frame itu (aman).

Strip **mulus** maupun **melompat per langkah** didukung: pergeseran global
diestimasi per frame dan posisi objek lama di-*snap* ke segmen teramati
(tanpa dead-reckoning drift). Jika strip tiba-tiba kosong (tombol clear),
tracker re-baseline dengan peringatan — objek lama tetap tidak ter-capture.

## API

| Endpoint | Fungsi |
|---|---|
| `POST /awb` | insert nomor AWB ke antrean WAITING |
| `GET /awb`, `GET /awb/waiting` | daftar AWB |
| `DELETE /awb/{id}` | **hapus permanen** AWB (WAITING/CANCELLED); AWB PAIRED ditolak (409) agar riwayat pairing utuh |
| `POST /session/start`, `POST /session/stop` | mulai/hentikan sesi scan (batch baru, resume idx dari DB) |
| `GET /session/state` | status sesi + overlay |
| `GET /dedup/review` | daftar REVIEW (crop baru vs kandidat lama + skor) |
| `POST /dedup/{id}/resolve` | `NOT_DUPLICATE` (→NEW+idx+AWB) / `CONFIRM_DUPLICATE` / `RESCAN_INTENTIONAL`; tercatat di `cv.review_audit` |
| `WS /ws/stream` | frame beranotasi + event `parcel.saved/duplicate/review/review_resolved`, `awb.*`, `session.*` |
| `GET /health` | status DB + model aktif |

Autentikasi opsional: set `CV_STREAM_TOKEN`, kirim via header
`x-operator-token` atau `?token=` (WebSocket & REST sama).

## Ganti model embedding

Cukup ubah config + re-embed — tanpa ubah kode:

```bash
.venv/bin/python tools/preload_models.py resnet50
# set CV_EMBEDDING__MODEL=resnet50 lalu:
.venv/bin/python tools/reembed.py          # bertahap, resumable (Ctrl-Aman)
```

Selama reembed berjalan, pencarian hanya memakai baris dengan model aktif —
vektor lintas model tidak pernah dicampur.

## Benchmark & kalibrasi (WAJIB sebelum produksi)

```bash
# siapkan crops per paket fisik: crops/<parcel_id>/*.png (bisa dari data/crops/)
.venv/bin/python tools/bench_embedders.py crops/ --models dinov2_small resnet18 resnet50 --out report/
# -> report/summary.md : AUC, EER, FAR/FRR, ms/gambar, uji orientasi/JPEG/noise/shift

# dari skor pasangan positif/negatif:
.venv/bin/python tools/calibrate_threshold.py pairs.json --target-far 0.001 --target-recall 0.98
```

## Hasil terukur (mesin ini)

- **Unit test**: 41 passed; **slow/integrasi** (dinov2_small nyata +
  Postgres/pgvector lokal): 5 passed.
- **E2E sintetis** (server nyata, pgvector, dinov2_small CPU): 14 paket →
  9 NEW idx 0–8 **tanpa lubang**, 5 DUPLICATE (sim 0.985–1.000) tanpa idx,
  3 AWB terpasang FIFO ke idx 0,1,2.
- **Latensi HTTP /health saat inferensi berjalan**: p50 ≈ 44 ms, p95 ≈ 52 ms,
  max ≈ 71 ms (termasuk overhead curl) — event loop tidak tersendat karena
  inferensi di worker thread terpisah. Streaming WebSocket tetap 10 fps.
- Penentuan idx & pairing AWB dalam satu transaksi Postgres dengan
  `pg_advisory_xact_lock` per batch — aman multi-instance, idempoten pada
  `(batch_id, track_id)`.

## Keterbatasan (jujur)

1. **Threshold `t_high=0.92 / t_low=0.80` MASIH SEMENTARA** — belum
   dikalibrasi dengan data X-ray asli dari scanner Anda. Jalankan
   benchmark + kalibrasi sebelum dipakai produksi.
2. **Model pretrained bukan model X-ray**: dinov2/resnet dilatih di foto
   natural. Untuk hard negative (paket beda isi, ukuran sama) akurasi bisa
   turun — kumpulkan data pasangan berlabel dari scanner sendiri; bila perlu
   fine-tune.
3. Segmentasi strip (profil kolom) menganggap **paket menempel tanpa celah**
   sebagai satu objek. Jika sering terjadi, gunakan mode `track`
   (YOLO11-seg + ByteTrack / MOG2) — logika dedup & AWB sama persis.
   Bobot YOLO default (COCO) tidak dilatih untuk X-ray; latih bobot sendiri.
4. Overlay magenta/teks UI di dalam ROI dapat mengganggu — batasi ROI dan
   jangan pakai foto monitor (moire); gunakan screen capture/HDMI bersih.
5. pgvector wajib untuk jalur utama; `CV_DEDUP__STORE=sqlite` tersedia untuk
   dev kecil tanpa Postgres (idx memakai lock proses, bukan advisory lock —
   single instance saja).
