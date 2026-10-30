"""
Omixa File Parser
Reads uploaded files into pandas DataFrames.
"""

import pandas as pd
import json
import io
from werkzeug.datastructures import FileStorage


SUPPORTED_EXTENSIONS = {
    'csv', 'tsv', 'xlsx', 'xls', 'xlsm', 'json', 'jsonl'
}


def get_extension(filename: str) -> str:
    return filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''


def parse_file(file: FileStorage) -> tuple[pd.DataFrame, str]:
    """
    Parse an uploaded file into a DataFrame.
    Returns (dataframe, detected_extension).
    Raises ValueError for unsupported/unparseable files.
    """
    ext = get_extension(file.filename)

    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '.{ext}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    raw = file.read()

    if ext == 'csv':
        df = _parse_csv(raw)
    elif ext == 'tsv':
        df = pd.read_csv(io.BytesIO(raw), sep='\t', on_bad_lines='skip')
    elif ext in ('xlsx', 'xlsm'):
        df = pd.read_excel(io.BytesIO(raw), engine='openpyxl')
    elif ext == 'xls':
        df = pd.read_excel(io.BytesIO(raw), engine='xlrd')
    elif ext == 'json':
        df = _parse_json(raw)
    elif ext == 'jsonl':
        df = _parse_jsonl(raw)
    else:
        raise ValueError(f"Parser not implemented for '.{ext}'")

    if df.empty:
        raise ValueError("The uploaded file appears to be empty.")

    return df, ext


def _parse_csv(raw: bytes) -> pd.DataFrame:
    """Try common CSV dialects."""
    for enc in ('utf-8', 'latin-1', 'cp1252'):
        for sep in (',', ';', '|'):
            try:
                df = pd.read_csv(
                    io.BytesIO(raw),
                    sep=sep,
                    encoding=enc,
                    on_bad_lines='skip',
                    low_memory=False,
                )
                if df.shape[1] > 1:  # at least 2 columns → right separator
                    return df
            except Exception:
                continue
    # last resort: single column
    return pd.read_csv(io.BytesIO(raw), on_bad_lines='skip')


def _parse_json(raw: bytes) -> pd.DataFrame:
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return pd.json_normalize(data)
        elif isinstance(data, dict):
            # try common nested structures
            for key in ('data', 'records', 'results', 'items'):
                if key in data and isinstance(data[key], list):
                    return pd.json_normalize(data[key])
            return pd.json_normalize([data])
        else:
            raise ValueError("JSON root must be an array or object")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")


def _parse_jsonl(raw: bytes) -> pd.DataFrame:
    lines = raw.decode('utf-8', errors='replace').strip().splitlines()
    records = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    if not records:
        raise ValueError("No valid JSON lines found in JSONL file")
    return pd.json_normalize(records)
