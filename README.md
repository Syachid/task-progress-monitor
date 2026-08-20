# Task Progress Monitor — Tim Indonesia

Memantau task SalesCRM yang dibuat tim Indonesia: mana yang **belum ada update**, mana yang
**overdue**, dan mana yang **belum punya due date**.

Ada dua jalur di repo ini. Keduanya memakai aturan bucket yang sama.

| | Laporan HTML | App FastAPI |
|---|---|---|
| Butuh install | tidak ada | Python 3.11+ |
| Butuh CRM API key | tidak | ya |
| Data | snapshot saat digenerate | tarik ulang kapan saja |
| Status | **jalan** | **jalan** (diverifikasi 20 Agustus 2026, `/api/verify` cocok) |

## Jalur 1 — Laporan HTML (tanpa install)

`Laporan-Task-Indonesia.html` — satu file mandiri, klik dua kali untuk buka. Isinya kartu
ringkasan yang bisa diklik, tabel yang bisa diurutkan, rekap per pembuat, dan ekspor CSV.

Untuk membuat ulang setelah data CRM berubah, tarik dulu data Task lewat MCP SalesCRM
(`list_records` pada `Task`, filter `record_type_id=100` + `task_type=Task`, `page_size=100`,
semua halaman) sehingga hasilnya tersimpan sebagai file di direktori `tool-results`, lalu:

```bash
powershell -ExecutionPolicy Bypass -File "tools\build_report.ps1"
```

Skrip membaca semua `*list_records*.txt` di direktori itu, membuang duplikat berdasarkan `id`,
menghitung bucket, dan menulis ulang file HTML-nya.

## Jalur 2 — App FastAPI

Struktur sudah mengikuti kontrak Substrait (`/health`, API di bawah `/api`, port 8000) supaya
naik ke platform nanti tinggal menambah Dockerfile + migrasi Flyway.

```bash
cp backend/.env.example backend/.env    # isi CRM_API_BASE_URL dan CRM_API_KEY
pip install -r backend/requirements.txt
uvicorn main:app --app-dir backend --port 8000
```

Buka `http://localhost:8000`. Endpoint: `/api/summary`, `/api/tasks` (filter `bucket`,
`creator`, `owner`, `manager`, `alex_direction`, `q`), `/api/creators`, `/api/export.csv`,
`POST /api/sync`, dan `/api/verify`.

**Jalankan `/api/verify` lebih dulu.** Endpoint itu membandingkan jumlah record yang dilihat
REST API dengan jumlah yang tercatat saat desain (233.707 Indonesia / 550 Task — `Note`
tidak lagi dilacak, lihat Catatan di bawah). Kalau meleset jauh, user pemilik API key punya
visibilitas berbeda — beresi itu sebelum mempercayai angka bucket mana pun.

`search_accounts` (dipakai untuk tag Hypercare/Strategic) sudah diverifikasi 20 Agustus
2026 — endpoint `GET /objects/Account/records?search=...` didukung REST API live CRM dan
mengembalikan set akun yang sama seperti hasil MCP. Lihat komentar di
`crm_client.search_accounts`.

Manager roster **tidak** di-fetch otomatis (kebijakan Google Workspace organisasi memblokir
share eksternal/link untuk sheet-nya) — upload manual lewat
`POST /api/manager-roster` (form-data field `file`, CSV dengan kolom `Name` dan `ASM`),
kira-kira sebulan sekali. Tanpa pernah diupload, kolom Manager selalu "Tanpa Manager".
Cek status/umur roster yang tersimpan lewat `GET /api/manager-roster`.

Tes logika bucket: `pytest test_buckets.py`

## Aturan bucket

Hanya berlaku untuk task yang **belum** `Completed`. Bucket tidak saling eksklusif.

| Bucket | Aturan |
|---|---|
| Belum pernah di-update | `version = 1`, atau `updated_at` kosong, atau `updated_at = created_at` |
| Stale | pernah di-update, tapi update terakhir > `STALE_DAYS` (default 14) hari lalu |
| Overdue | `due_date` sudah lewat **dan** `due_date >= created_at` |
| Tanpa due date | `due_date` kosong |
| Due date janggal | `due_date` lebih awal dari `created_at` (dikeluarkan dari Overdue) |

Semua perhitungan tanggal memakai **Asia/Jakarta**.

Tiap task juga dapat **tag** (`MUST WIN` dari `lead_source_detail` opportunity;
`HYPERCARE`/`STRATEGIC` dari kolom Customer Success Manager di parent Account) dan
**manager** (dari Google Sheet "ID-User Master", tab List, kolom ASM — fallback
"Tanpa Manager" kalau owner tidak ada di roster atau ASM-nya kosong).

## Catatan data CRM (hasil verifikasi 14 Agustus 2026)

- **Indonesia = `record_type_id: 100`.** Labelnya ada di `record_type_name`, tapi field itu
  **tidak bisa dipakai sebagai filter** (mengembalikan 0) — selalu filter pakai id numeriknya.
- Dari 233.707 record Indonesia, hanya **565 yang CRM-native** (550 `Task` + 15 `Note`).
  Sisanya 233.142 adalah impor Salesforce yang ditandai `sf_id` dan dikecualikan.
- Data memuat status **`Open`** yang tidak ada di picklist `describe_object`. Jangan
  menganggap picklist itu lengkap; perlakukan semua yang bukan `Completed` sebagai terbuka.
- `due_date` datang dalam tiga bentuk: `2026-08-14T12:12:00Z` (beroffset),
  `2024-12-16T00:00:00` (naif — diperlakukan sebagai WIB), dan `2025-12-31` (tanggal saja —
  diperlakukan sebagai akhir hari WIB).
- Endpoint list **tidak** mengirim `created_by_name`. Nama pembuat diturunkan dari baris yang
  `owner_id`-nya sama dengan `created_by` (sudah dicek benar lewat `get_record`).
- **PowerShell 5.1 wajib pakai `-Encoding UTF8`** saat membaca file tanpa BOM, kalau tidak
  teks non-ASCII di deskripsi task akan rusak.
