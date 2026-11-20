# Instalasi cv-service di Windows

Jalankan perintah berikut di **PowerShell** dari folder repo yang berisi `app`, `ui`, dan `requirements.txt`.

## 1. Prasyarat

- Python **3.11 64-bit** dengan Python Launcher (`py`).
- Node.js **22.12.0 atau lebih baru** dengan npm.
- Git, jika repo belum di-clone.
- PostgreSQL dengan ekstensi **pgvector** jika menggunakan database operasional. Untuk uji lokal, gunakan SQLite.

```powershell
py -3.11 --version
node --version
npm.cmd --version
```

## 2. Buat virtual environment dan instal Python

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Untuk menjalankan tes, instal juga dependensi pengembangan.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Perintah di panduan ini memakai Python dari `.venv` secara langsung. Jika ingin mengaktifkannya di PowerShell, jalankan `.\.venv\Scripts\Activate.ps1`.

## 3. Atur konfigurasi

Jika belum ada `.env`, buat dari contoh.

```powershell
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
}
notepad .env
```

Isi `CV_JWT_SECRET` dengan nilai acak. Perintah berikut menghasilkan nilai yang bisa disalin ke `.env`.

```powershell
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

Isi juga `CV_BOOTSTRAP_PASSWORD` dengan kata sandi awal. Nilai ini hanya dipakai saat membuat akun bawaan yang belum ada. Ubah baris konfigurasi yang sudah tersedia dan simpan `.env` di mesin lokal.

Untuk **uji lokal dengan SQLite** dan pipeline dua kamera, gunakan pengaturan berikut.

```dotenv
CV_DEDUP__STORE=sqlite
CV_DB_TUNNEL_ENABLED=false
CV_PIPELINE_MODE=track
CV_DETECTOR__BACKEND=yolo
CV_DETECTOR__WEIGHTS=yolo26n-seg.pt
CV_CAPTURE__TOP_SOURCE=screen
CV_CAPTURE__SIDE_SOURCE=screen
```

Untuk **PostgreSQL**, gunakan `CV_DEDUP__STORE=pgvector` dan isi `CV_DATABASE_URL` dengan DSN database yang sudah disiapkan. Backend menjalankan migrasi saat startup. Jika instalasi menggunakan tunnel SSH bawaan, isi konfigurasi `CV_DB_TUNNEL_*` sesuai akses yang diberikan dan arahkan DSN ke `127.0.0.1:15432`. Gunakan pengaturan akses dan host key yang diberikan pengelola server.

## 4. Unduh bobot model

Jalankan sekali saat koneksi internet tersedia. Perintah ini mengunduh bobot embedding dan detektor bawaan di atas.

```powershell
.\.venv\Scripts\python.exe tools\preload_models.py dinov2_small yolo26n-seg
```

## 5. Instal dan build frontend

```powershell
Set-Location ui
npm.cmd ci
npm.cmd run build
Set-Location ..
```

`npm.cmd ci` menginstal dependensi sesuai lockfile. Hasil build ada di `ui/dist` dan disajikan langsung oleh FastAPI.

## 6. Jalankan backend

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8100
```

Setelah startup selesai, buka [halaman Live](http://127.0.0.1:8100/live). Login menggunakan akun gudang yang sudah dikonfigurasi. Pada database baru, akun bawaan adalah `bgd@paratekno.com` dan `cas@paratekno.com` dengan kata sandi awal dari `CV_BOOTSTRAP_PASSWORD`.

Untuk uji Screen Share, pilih sumber video atas dan samping di halaman Live, lalu klik **Start Session**. Tekan **Ctrl+C** di terminal untuk menghentikan backend.

## 7. Jalankan otomatis sebagai Windows service (NSSM)

Agar backend menyala otomatis sesaat setelah PC boot (tanpa perlu login/manual), daftarkan sebagai Windows service memakai **NSSM**. Service berjalan sebagai `LocalSystem`, start otomatis, dan restart sendiri jika crash.

### 7.1. Instal NSSM

```powershell
winget install --id NSSM.NSSM -e --accept-source-agreements --accept-package-agreements
```

### 7.2. Buat & start service

Skrip siap pakai ada di `logs\install-cv-service.ps1`. Jalankan di **PowerShell as Administrator**:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\xraypy\logs\install-cv-service.ps1
```

Skrip ini mendaftarkan service `CVService` dengan konfigurasi:

- App: `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8100`
- Direktori kerja: `C:\xraypy` (agar `.env` termuat).
- `HF_HOME` diarahkan ke cache HuggingFace pengguna agar bobot `dinov2_small` tidak diunduh ulang.
- Start type: `Automatic` (saat boot).
- Restart otomatis jika aplikasi keluar/crash (`AppExit Default Restart` + kebijakan failure `sc.exe`).
- Log: `C:\xraypy\logs\cv-service.out.log` dan `cv-service.err.log` (rotasi 10 MB / harian).

Hasil akhir skrip tertulis di `logs\install-cv-service.sentinel` (`OK` bila port 8100 siap).

### 7.3. Pengelolaan (PowerShell biasa)

```powershell
Get-Service CVService                                    # cek status
Start-Service CVService
Stop-Service CVService
Restart-Service CVService
Get-Content C:\xraypy\logs\cv-service.out.log -Tail 50 -Wait   # tail log live
```

Setelah service aktif, buka [halaman Live](http://127.0.0.1:8100/live).

### 7.4. Uninstall (PowerShell as Administrator)

```powershell
$nssm = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\NSSM.NSSM*\win64\nssm.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
& $nssm.FullName remove CVService confirm
```
