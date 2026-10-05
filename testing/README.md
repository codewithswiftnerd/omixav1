# Smoke-testing the Omixa backend (no frontend needed)

Server must be running first: `python app.py`

A sample file with duplicates, a blank row, and stray whitespace is
included at `testing/sample.csv`, good for actually seeing the
cleaning rules do something. For the gender/country/boolean-flag/
phone-punctuation/scientific-notation/age-validation improvements,
use `testing/messy_sample_v2.csv` instead, it exercises every rule
in `cleaning/rules.py`, including an intentionally ambiguous date
column and an unrecoverable scientific-notation phone value.

Automated tests (once pytest is installed, `pip install pytest`)
live in `testing/test_omixa.py` (V1 core behavior) and
`testing/test_omixa_v2.py` (the gender/country/phone/boolean-flag/
summary additions): `pytest testing/ -v`.

---

## 1. Health check (browser or curl)

```
curl http://127.0.0.1:5000/api/health
```
Expect: `{"status":"ok","service":"omixa-backend"}`

---

## 2. Upload a file

```
curl -X POST http://127.0.0.1:5000/api/upload/ \
  -F "file=@testing/sample.csv"
```
Expect something like:
```json
{"job_id":"b3f1...", "filename":"sample.csv", "status":"uploaded"}
```
**Copy the `job_id`, you need it for the next two steps.**

---

## 3. Process it

```
curl -X POST http://127.0.0.1:5000/api/process/<job_id>
```
(replace `<job_id>` with what you got back from step 2)

Expect:
```json
{
  "job_id": "b3f1...",
  "status": "completed",
  "summary": {
    "rows_in": 5,
    "rows_out": 3,
    "rules_applied": ["missing_values", "duplicates", "formatting"],
    "changes": {
      "missing_values_changed": 1,
      "duplicates_changed": 1,
      "formatting_changed": 2
    }
  }
}
```

To test only specific rules:
```
curl -X POST http://127.0.0.1:5000/api/process/<job_id> \
  -H "Content-Type: application/json" \
  -d "{\"rules\": [\"duplicates\"]}"
```

---

## 4. Download the cleaned file

```
curl -OJ http://127.0.0.1:5000/api/download/<job_id>
```
This saves `cleaned.csv` to your current folder. Open it, the
duplicate row and the blank row should be gone, and "Mike Ross "
should have its whitespace trimmed.

Running download again for the same `job_id` will now 404, that's correct, the temp folder gets deleted right after download.

---

## Postman

Import `testing/omixa.postman_collection.json` instead if you'd
rather click buttons than type curl. It has all four requests
pre-built; you'll still need to paste the `job_id` from step 2 into
steps 3 and 4 (or set it as a collection variable).
