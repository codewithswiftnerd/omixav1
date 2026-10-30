# Omixa — Data Cleaning Backend

Flask-based REST API that cleans and standardizes messy datasets across healthcare, finance, and general domains.

---

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Server runs at `http://localhost:5000`

---

## API Reference

### `GET /api/health`
Health check.

---

### `POST /api/upload`
Upload a dataset file.

**Request:** `multipart/form-data`
| Field | Type | Description |
|-------|------|-------------|
| `file` | File | CSV, TSV, XLSX, XLS, JSON, or JSONL |

**Response:**
```json
{
  "session_id": "uuid",
  "filename": "patients.csv",
  "file_type": "csv",
  "profile": { "rows": 1000, "cols": 12, "missing_pct": 4.2, ... },
  "message": "File uploaded successfully. 1000 rows, 12 columns detected."
}
```

---

### `POST /api/clean`
Run the cleaning pipeline on an uploaded dataset.

**Request body (JSON):**
```json
{
  "session_id": "uuid",
  "domain": "healthcare",
  "missing_strategy": "auto",
  "outlier_action": "flag"
}
```

| Field | Options | Default | Description |
|-------|---------|---------|-------------|
| `domain` | `healthcare`, `finance`, `general` | `general` | Activates domain-specific rules |
| `missing_strategy` | `auto`, `drop_rows`, `fill_zero` | `auto` | How to handle missing values |
| `outlier_action` | `flag`, `cap`, `remove` | `flag` | What to do with outliers |

**Response:**
```json
{
  "session_id": "uuid",
  "report": {
    "domain": "healthcare",
    "original_shape": [1000, 12],
    "final_shape": [987, 11],
    "rows_removed": 13,
    "cols_removed": 1,
    "duration_ms": 142.3,
    "steps": {
      "column_names": ["'Patient ID' → 'patient_id'"],
      "duplicates": { "removed": 5 },
      "missing_values": { "age": { "missing": 8, "pct": 0.8, "action": "filled_with_median (34.0)" } },
      "data_types": { "dob": "object → datetime" },
      "strings": { "email": ["1 invalid emails nulled"] },
      "outliers": { "weight": { "outliers": 3, "action": "flag" } },
      "domain_rules": ["Standardized gender values in 'gender'"]
    }
  },
  "preview": [...],
  "columns": ["patient_id", "age", ...],
  "message": "Cleaning complete. 13 rows and 1 columns removed. Took 142.3ms."
}
```

---

### `GET /api/export/<session_id>?format=csv`
Download the cleaned dataset.

| Param | Options | Default |
|-------|---------|---------|
| `format` | `csv`, `xlsx`, `json` | `csv` |

Returns a file download.

---

### `GET /api/profile/<session_id>?stage=original`
Get a data quality profile.

| Param | Options | Default |
|-------|---------|---------|
| `stage` | `original`, `cleaned` | `original` |

---

## What Gets Cleaned

| Step | What happens |
|------|-------------|
| **Column names** | Lowercased, spaces → underscores, special chars stripped |
| **Duplicates** | Exact duplicate rows removed |
| **Missing values** | Numeric → median fill; Categorical → mode fill; >60% missing → column dropped |
| **Data types** | Strings to datetime/numeric where detected; currency symbols stripped |
| **Strings** | Emails validated/lowercased; phones normalized; text title-cased |
| **Outliers** | Z-score based; flag/cap/remove per config |
| **Domain rules** | Healthcare: age/BMI sanity, gender standardization. Finance: currency codes uppercased, negative amount warnings |

---

## Typical Flow

```
POST /api/upload        →  get session_id + profile
POST /api/clean         →  get report + preview
GET  /api/export/{id}   →  download cleaned file
```

---

## Accounts (auth)

| Endpoint | Body | Notes |
|----------|------|-------|
| `POST /api/auth/signup` | `{name, email, password}` | password ≥ 8 chars; returns `{token, user}` |
| `POST /api/auth/login` | `{email, password}` | returns `{token, user}` |
| `POST /api/auth/google` | `{credential}` | needs `GOOGLE_CLIENT_ID` env var |
| `POST /api/auth/forgot-password` | `{email}` | reset link is logged until email sending is wired |
| `POST /api/auth/reset-password` | `{token, password}` | |
| `GET /api/auth/me` | header `Authorization: Bearer <token>` | |

Env vars: `OMIXA_SECRET_KEY` (required in production), `GOOGLE_CLIENT_ID`, `FRONTEND_URL`, `OMIXA_DB_PATH`.
Users have `plan` (default `free`) and `pro_until` columns for Pro billing; auth never modifies them.
