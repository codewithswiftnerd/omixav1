"""Public guide pages (one per search intent) and the About page.

Every sentence here was checked against what the product does today (docs/CLEANING_ENGINE.md,
docs/EXCEL_FIDELITY.md, templates/landing.html FAQ, templates/pricing.html). Do not add a claim the code does not
back. Numbers come from product_facts() so they cannot drift from the enforced limits.

Intent map (one page per intent, no overlap):
  clean-csv-files               -> cleaning CSV files
  clean-excel-files             -> cleaning Excel (.xlsx/.xls) files
  remove-duplicate-rows         -> finding and removing duplicate rows
  handle-missing-values         -> blanks, N/A, placeholders, filling gaps
  data-validation               -> checking values against what a column should hold
  data-quality-assessment       -> scoring and reporting spreadsheet quality
  standardize-spreadsheet-data  -> consistent dates, phones, emails, categories, names
  prepare-data-for-research     -> getting a dataset ready for analysis, with a change record
"""
from flask import Blueprint, abort, render_template

from routes.pages import product_facts

guides_bp = Blueprint("guides", __name__)

GUIDES = {
    "clean-csv-files": {
        "nav": "Clean CSV files",
        "title": "How to Clean a CSV File Online | Omixa",
        "description": "Clean a messy CSV file: stray spaces, duplicate rows, inconsistent dates and placeholders. Review the changes and download a cleaned copy. Free, no account.",
        "h1": "How to clean a CSV file",
        "lead": "CSV files are easy to create and easy to get wrong. Exports from different systems disagree about dates, spaces, blanks and capitalisation, and the problems only show up when you try to analyse the file. Omixa checks the file, fixes what is safe to fix, and flags the rest for you to decide.",
        "sections": [
            ("What goes wrong in CSV files", [
                "Most CSV problems are small but numerous: values padded with spaces, the same category written three ways (Male, MALE, male), dates in several formats, phone numbers with mixed punctuation, and \"N/A\", \"null\" or \"-\" standing in for a missing value. Exact duplicate rows are common when files are merged or exported twice.",
                "None of these stop a CSV from opening, which is why they survive until a pivot table, a join or a chart gives the wrong answer."]),
            ("What Omixa does with a CSV", [
                "Upload a .csv file (up to {free_mb} MB on Free, {pro_mb} MB with Pro) and Omixa reads it cell by cell. It reports what it found with a data-quality score, applies the safe fixes you leave switched on, and gives you a before and after preview and a change log.",
                "Only a truly empty cell is treated as missing when the file is read. Values such as N/A, null and NA are recognised as placeholders by a rule you can see, so each conversion is logged rather than happening silently. Whole numbers stay whole (45 does not become 45.0), and ID-like columns such as account numbers and zip codes are protected from changes that would drop leading zeros."]),
            ("Your original stays untouched", [
                "Omixa never edits the file you uploaded. It writes a separate cleaned copy, and then re-reads that copy to check it against the cleaned data. Cells nobody changed read exactly as they did in your upload. Files are deleted automatically, at most {ttl_minutes} minutes after upload."]),
        ],
        "steps": ["Upload your CSV, or try one of the sample files.", "Read the findings and the quality score.", "Choose which cleaning rules to apply and review any flagged values.", "Download the cleaned CSV and keep the change log with it."],
        "limits": "Files with more than {max_rows} rows or {max_cells} cells are not processed. Ambiguous values (for example a date such as 03/04/2025 that could be day-first or month-first) are left exactly as uploaded and flagged, not guessed.",
        "faq": [
            ("Is it free to clean a CSV file with Omixa?", "Yes. Cleaning one file at a time is free and needs no account. Omixa Pro adds batch processing, Quality Profiles and PDF quality reports."),
            ("Will Omixa change my data without asking?", "Only safe, well-defined fixes are applied by default. Filling gaps with estimates is off unless you turn it on, and anything ambiguous is flagged for your review."),
            ("What happens to my file after cleaning?", "It is removed automatically, at most {ttl_minutes} minutes after upload. Omixa keeps basic records such as file name, size, row counts and quality scores, not your cell contents.")],
        "related": ["remove-duplicate-rows", "handle-missing-values", "standardize-spreadsheet-data"],
    },
    "clean-excel-files": {
        "nav": "Clean Excel files",
        "title": "How to Clean an Excel File (.xlsx, .xls) | Omixa",
        "description": "Clean an Excel workbook: trim spaces, remove duplicate rows, fix dates and formats, and get a quality report. See exactly what is kept and what is not.",
        "h1": "How to clean an Excel file",
        "lead": "Excel files collect problems over years of copy, paste and manual edits. Omixa cleans the data in your spreadsheet, shows what changed, and is upfront about what a cleaned workbook keeps and what it does not.",
        "sections": [
            ("What Omixa cleans in a workbook", [
                "Omixa reads the first sheet of an .xlsx or .xls file, finds issues such as stray spaces, duplicate rows, inconsistent dates, mixed capitalisation, numbers stored as text and placeholder values, and applies the fixes you choose. Files up to {free_mb} MB are free to clean, and {pro_mb} MB with Pro."]),
            ("What stays and what does not", [
                "Omixa rebuilds the cleaned sheet rather than editing the workbook in place, so it keeps what it can map safely and says plainly what it drops.",
                "Kept: cleaned cell values, other sheets and their order, workbook properties, column widths, fonts, hidden columns and a frozen header row.",
                "Not kept: formulas (you get their values), merged cells, hyperlinks, comments, conditional formatting, data validation, Excel tables, row heights, fills and number formats. Cleaning can remove rows and rename columns, so features tied to specific cells would end up pointing at the wrong place.",
                "Macro-enabled workbooks (.xlsm) are rejected at upload, and .xls files are returned as .xlsx."]),
            ("Why that matters", [
                "If your workbook relies on formulas or formatting, clean a copy of the data and keep your original workbook as the working file. If you need clean values for analysis, this is usually what you want."]),
        ],
        "steps": ["Upload the .xlsx or .xls file.", "Review the findings and the quality score.", "Choose rules and resolve any flagged values.", "Download the cleaned .xlsx and compare it with the preview."],
        "limits": "Only the first sheet is cleaned; other sheets are carried over unchanged. Files with more than {max_rows} rows or {max_cells} cells are not processed.",
        "faq": [
            ("Does Omixa keep my Excel formatting?", "Partly. It keeps values, other sheets, column widths, fonts, hidden columns and a frozen header row. Formulas, merged cells, hyperlinks, comments, conditional formatting, data validation, tables, row heights, fills and number formats are not carried over."),
            ("Can I clean .xls files?", "Yes. Older .xls files are accepted and returned as .xlsx."),
            ("Can I clean several Excel files at once?", "Yes, with Omixa Pro. Batch processing cleans up to {batch_files} files per batch, and each file is cleaned and reported on separately.")],
        "related": ["clean-csv-files", "remove-duplicate-rows", "data-quality-assessment"],
    },
    "remove-duplicate-rows": {
        "nav": "Remove duplicate rows",
        "title": "How to Remove Duplicate Rows from a Spreadsheet | Omixa",
        "description": "Find and remove exact duplicate rows in CSV and Excel files, and review near-duplicates safely. The first occurrence is kept and every removal is logged.",
        "h1": "How to remove duplicate rows from a spreadsheet",
        "lead": "Duplicate rows inflate counts, totals and averages. Removing them sounds simple, but deleting the wrong rows is worse than leaving them in. Omixa separates duplicates that are safe to remove from those that need a person to decide.",
        "sections": [
            ("Exact duplicates, removed", [
                "An exact duplicate is a row where every cell matches an earlier row. When you choose to remove duplicates, Omixa keeps the first occurrence, removes the repeats, and records each removal in the change log with the row it came from."]),
            ("Near-duplicates, reviewed", [
                "Some rows look like duplicates but are not. Omixa reports two kinds for review and never removes them automatically: rows that match on the key columns you choose but differ elsewhere, and rows that share an email address or phone number but are not exact copies.",
                "A shared name is never treated as proof of identity. Two people called Ada Okafor are not one person, so a name match alone is not a duplicate."]),
            ("Checking the result", [
                "The quality report counts duplicates found and removed, so you can see how much of the file they affected. Duplicates also lower the data-quality score, which is why an exact-repeat share is part of how the score is calculated."]),
        ],
        "steps": ["Upload your CSV or Excel file.", "Check how many exact duplicates were found.", "Leave \"Remove duplicates\" on to keep the first occurrence of each row.", "Review the near-duplicate flags and decide each one yourself."],
        "limits": "Removal applies to exact duplicate rows only. Rows that differ in any cell are not removed automatically.",
        "faq": [
            ("Which duplicate is kept?", "The first occurrence stays. Later identical rows are removed and logged."),
            ("Will Omixa remove people who share a name?", "No. Matching names are not treated as duplicates. Only exact repeated rows are removed automatically."),
            ("Can I see what was removed?", "Yes. The change log lists each rule that ran and how many cells or rows it changed, and removals are recorded individually.")],
        "related": ["clean-csv-files", "data-quality-assessment", "standardize-spreadsheet-data"],
    },
    "handle-missing-values": {
        "nav": "Handle missing values",
        "title": "How to Handle Missing Values in a Dataset | Omixa",
        "description": "Spot blanks and placeholders like N/A, null and dashes, and decide whether to leave them or fill them. Omixa never fills gaps with estimates unless you ask.",
        "h1": "How to handle missing values in a dataset",
        "lead": "Missing values are rarely just empty cells. They show up as N/A, null, a dash, TBD or a typed \"not known\", and each system spells them differently. Before deciding what to do about gaps, you need to be able to see them all.",
        "sections": [
            ("Finding every kind of missing value", [
                "Omixa treats truly empty cells as missing and recognises common placeholders such as N/A, NA, null, None, a dash, TBD and \"not available\" as missing too. The recognition is a visible rule, and each conversion is written to the change log.",
                "Missing values count against quality. A column that matters to your analysis and is largely blank will pull the data-quality score down, and the findings show where the gaps are."]),
            ("Deciding what to do about them", [
                "There is no single right answer. Leaving a gap empty is honest. Filling it is a modelling choice that changes your results.",
                "Omixa keeps that choice with you. Filling gaps with estimates (the median for numbers, \"Unknown\" for text) is off by default and only runs if you switch it on, because those values are estimates, not data you collected."]),
            ("Guidance, not guesses", [
                "Before filling anything, ask whether the gaps are random or follow a pattern, whether a blank means \"none\" or \"not recorded\", and whether the column is needed at all. The change log and report let you document whichever approach you choose."]),
        ],
        "steps": ["Upload your file and read the missing-value findings.", "Keep \"Recognize blanks\" on so placeholders are counted as missing.", "Decide whether to leave gaps or turn on \"Fill gaps with estimates\".", "Download the cleaned file and keep the change log."],
        "limits": "Estimates are never applied unless you turn them on. Omixa does not decide which approach is statistically right for your study.",
        "faq": [
            ("Does Omixa fill in missing values automatically?", "No. Filling gaps with estimates is off by default and only happens if you choose it."),
            ("Which placeholders count as missing?", "Empty cells and common placeholders such as N/A, null, a dash, TBD and similar worded markers."),
            ("Will placeholder conversion be recorded?", "Yes. Every conversion appears in the audit log, so you can see exactly what was treated as missing.")],
        "related": ["data-quality-assessment", "data-validation", "prepare-data-for-research"],
    },
    "data-validation": {
        "nav": "Data validation",
        "title": "Spreadsheet Data Validation: Catch Bad Values | Omixa",
        "description": "Check CSV and Excel data for values that do not fit their column, like words in phone numbers, impossible ages and future dates. Pro adds Quality Profiles.",
        "h1": "Data validation for spreadsheets",
        "lead": "A value can be neatly formatted and still be wrong. Data validation checks whether values make sense for the column they are in, so problems are found before they reach your analysis.",
        "sections": [
            ("What gets checked", [
                "Omixa looks for values that do not fit their column: words in a phone number column, ages below zero or above 120, an age of exactly zero (often a placeholder), negative quantities, and sign-up dates in the future. It also suggests likely misspellings, such as Lagoss for Lagos, without applying the correction.",
                "These checks are plausibility warnings. A suspicious value is flagged for you, not changed, and the original is kept."]),
            ("Setting your own standard with Quality Profiles", [
                "With Omixa Pro you can describe the standard a dataset must meet once, save it as a Quality Profile, and check any file against it. Each check returns a PASS, WARNING or FAIL result, with column-level quality scores for deeper analysis."]),
            ("Validation is not correction", [
                "Consistent-looking data is not the same as correct data. When Omixa cannot tell whether a value is an error, it reports it and leaves the decision to you."]),
        ],
        "steps": ["Upload your file and open the findings.", "Review values flagged as invalid or suspicious.", "Accept, reject or keep each flagged value as it is.", "With Pro, save the standard as a Quality Profile and reuse it."],
        "limits": "Quality Profiles are an Omixa Pro feature. Free checks flag issues but do not know your organisation's own rules.",
        "faq": [
            ("What is a Quality Profile?", "A saved description of the standard a dataset must meet. Pro users check files against it and get a PASS, WARNING or FAIL result."),
            ("Does validation change my data?", "Flagged values are left as they were unless you choose a change."),
            ("Can I set my own rules?", "Yes, with Omixa Pro, through Quality Profiles and per-column rules.")],
        "related": ["data-quality-assessment", "handle-missing-values", "standardize-spreadsheet-data"],
    },
    "data-quality-assessment": {
        "nav": "Data quality assessment",
        "title": "Data Quality Assessment for CSV and Excel Files | Omixa",
        "description": "Get a data-quality score and report for a spreadsheet: what was found, what was fixed, and what still needs review. Practical, explainable, no guesswork.",
        "h1": "Data quality assessment for spreadsheets",
        "lead": "How good is this dataset, really? A data-quality assessment answers that with evidence: what is wrong, how much of the data it affects, and what has been fixed.",
        "sections": [
            ("How the score works", [
                "Omixa's score is cell-based. Each column gets a score from the share of its cells that are clean, and the file score combines them, giving more weight to columns that matter more, such as identifiers. A large share of exact duplicate rows lowers it, and a serious unresolved finding limits the grade label.",
                "Missing, invalid, inconsistent and suspicious values each count against the cells they affect, so the score follows from the findings and does not float free of them."]),
            ("An honest report", [
                "The report separates what was detected, what was corrected automatically, what remains, and what needs your review. Omixa re-checks the final dataset before reporting, so the numbers describe the file you download. A file is only called fully clean when nothing is waiting for a person."]),
            ("What a high score does not mean", [
                "A high score means the data is consistent and plausible. It does not prove the values are true. Treat it as a guide to where to look, not a certificate."]),
            ("With Omixa Pro", [
                "Pro adds column-level quality scores, a \"what changed?\" report and change log, saved processing history (scores and change summaries, never your rows), and a professional PDF quality report."]),
        ],
        "steps": ["Upload your CSV or Excel file.", "Read the score and what it is based on.", "Open the findings to see which columns and values drive it.", "Clean, then compare the score and report before and after."],
        "limits": "The score reflects the checks Omixa runs. It cannot tell you whether a value is factually correct.",
        "faq": [
            ("What does the quality score measure?", "How many cells in each column are clean, weighted by how much each column matters, adjusted for duplicate rows and serious unresolved findings."),
            ("Is the report available on the Free plan?", "Free includes a data-quality score and issue detection. Pro adds column-level scores, a change report and a PDF quality report."),
            ("Does my data get stored?", "Files are deleted automatically, at most {ttl_minutes} minutes after upload. Pro history keeps quality figures and change summaries, never your rows or example values.")],
        "related": ["data-validation", "remove-duplicate-rows", "prepare-data-for-research"],
    },
    "standardize-spreadsheet-data": {
        "nav": "Standardize spreadsheet data",
        "title": "How to Standardize Spreadsheet Data: Dates, Phones, Names | Omixa",
        "description": "Make spreadsheet values consistent: dates, phone numbers, emails, countries, categories and capitalisation. Ambiguous values are flagged, not guessed.",
        "h1": "How to standardize spreadsheet data",
        "lead": "Standardizing means the same thing is written the same way every time, so filtering, grouping and joining behave. Omixa applies consistent formats to the values it can read safely and flags the ones it cannot.",
        "sections": [
            ("What can be standardized", [
                "Dates that can only be read one way become YYYY-MM-DD. Phone numbers lose stray spaces, brackets and hyphens. Emails are trimmed and lowercased. Countries (NG, nigeria) resolve to one name, gender and yes/no spellings are unified, currency labels such as naira and ₦ become NGN, and categories that differ only by case, spaces, dots or hyphens are merged.",
                "Capitalisation is fixed where it is clearly inconsistent: names typed in one case become Title Case, and ID prefixes follow the column's majority. Mixed-case names such as McDonald or O'Brien are never touched."]),
            ("What is left alone on purpose", [
                "A date like 03/04/2025 could mean 3 April or 4 March. Omixa does not guess from other cells: ambiguous, impossible and placeholder dates stay exactly as uploaded and the column is flagged. Column names are only changed if you choose \"Standardize column names\", which turns Customer Name into customer_name."]),
            ("Control with Omixa Pro", [
                "Pro adds per-column rules and saved cleaning profiles, so you can set a rule once, such as the phone country or a custom replacement, and apply it to every file."]),
        ],
        "steps": ["Upload your file and read the findings.", "Pick the standardizing rules you want.", "Resolve ambiguous values you are asked about.", "Download the file and check the before and after preview."],
        "limits": "Only values that can be read safely are rewritten. Ambiguous values are reported for you to decide.",
        "faq": [
            ("Which date format does Omixa use?", "Clear dates are written as YYYY-MM-DD. Ambiguous ones are left unchanged and flagged."),
            ("Does Omixa standardize phone numbers for different countries?", "Yes. It supports Nigerian and international formats, and asks for a country when a number needs one."),
            ("Can I undo a change?", "Your original file is never modified, and you can re-run from it with different rules.")],
        "related": ["clean-csv-files", "data-validation", "prepare-data-for-research"],
    },
    "prepare-data-for-research": {
        "nav": "Prepare data for research",
        "title": "Prepare a Dataset for Research and Analysis | Omixa",
        "description": "Get survey and research data ready for analysis with a clean copy, a change log and flagged items for your review. Built for analysts, students and NGOs.",
        "h1": "How to prepare a dataset for research and analysis",
        "lead": "Cleaning is part of your method. Whether you are a student with survey responses, an analyst with exported records, or an organisation consolidating field data, you need clean data and a record of what was done to it.",
        "sections": [
            ("Keep the raw data raw", [
                "Omixa never modifies the file you upload. It produces a separate cleaned copy, so the original stays available as your source of truth."]),
            ("Keep a record of every change", [
                "A before and after preview and a change log show each rule that ran and how many cells it changed, with the source row for each change. That gives you something concrete to cite when someone asks how the data was prepared."]),
            ("Decide what is ambiguous yourself", [
                "Omixa applies safe fixes and flags the rest: impossible ages, mixed date conventions, unfamiliar category spellings, values that do not fit their column. It does not fill gaps with estimates unless you opt in, which keeps analytical decisions with the analyst."]),
            ("When you handle many files", [
                "Omixa Pro cleans up to {batch_files} files in one batch, each cleaned and reported on separately, and produces a PDF quality report. Pro history stores scores and change summaries, never your rows."]),
        ],
        "steps": ["Keep your original export untouched.", "Upload it and read the findings.", "Apply safe fixes and resolve flagged values.", "Save the cleaned file and the change log together."],
        "limits": "Omixa supports data preparation. It does not replace statistical judgement, and a clean file is not automatically a valid one.",
        "faq": [
            ("Is Omixa suitable for survey data?", "Yes for the cleaning side: consistent categories, dates, numbers and missing-value handling, with a change log. You still decide how to analyse it."),
            ("Can I show how my data was cleaned?", "The change log and before and after preview record the rules applied. Pro adds a PDF quality report."),
            ("Do I need an account?", "Not for Free cleaning. Pro features need an account.")],
        "related": ["handle-missing-values", "data-quality-assessment", "clean-excel-files"],
    },
}

GUIDE_ORDER = list(GUIDES)


def guide_nav() -> list[tuple[str, str]]:
    return [(f"/{slug}", GUIDES[slug]["nav"]) for slug in GUIDE_ORDER]


def _fmt(value, facts):
    if isinstance(value, str):
        return value.format(**facts)
    if isinstance(value, dict):
        return {k: _fmt(v, facts) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_fmt(v, facts) for v in value)
    return value


def render_guide(slug: str):
    g = GUIDES.get(slug)
    if g is None:
        abort(404)
    facts = product_facts()
    page = _fmt({k: v for k, v in g.items() if k != "related"}, facts)
    related = [{"path": f"/{s}", "label": GUIDES[s]["nav"], "blurb": GUIDES[s]["description"]} for s in g["related"]]
    return render_template("guide.html", g=page, related=related, slug=slug, **facts)


def _make_view(slug):
    def view():
        return render_guide(slug)
    view.__name__ = f"guide_{slug.replace('-', '_')}"
    return view


for _slug in GUIDE_ORDER:
    guides_bp.add_url_rule(f"/{_slug}", endpoint=_slug, view_func=_make_view(_slug), methods=["GET"])


@guides_bp.get("/about")
def about():
    return render_template("about.html", **product_facts())
