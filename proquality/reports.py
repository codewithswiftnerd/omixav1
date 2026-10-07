"""Downloadable Pro outputs built from a stored session: PDF quality report, "What changed" text, change-log CSV."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from xml.sax.saxutils import escape

_GLYPH = {"ok": "[OK]", "review": "[REVIEW]"}
_STATUS_MARK = {"PASS": "PASS", "WARNING": "WARNING", "FAIL": "FAIL"}


def _when(ts) -> str:
    return datetime.fromtimestamp(ts or 0, tz=timezone.utc).strftime("%d %b %Y, %H:%M UTC")


def _size(n) -> str:
    n = n or 0
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


def what_changed_text(session: dict) -> str:
    wc = session["report"]["what_changed"]
    lines = ["WHAT CHANGED", f"Dataset: {session['dataset_name']}", f"Processed: {_when(session['created_at'])}", ""]
    for l in wc["lines"]:
        lines.append(f"{_GLYPH.get(l['status'], '-')} {l['text']}")
    items = session["report"].get("review_items") or []
    if items:
        lines += ["", "NEEDS YOUR REVIEW"]
        for i in items:
            col = f" ({i['column']})" if i.get("column") else ""
            lines.append(f"- {i['title']}{col}: {i['count']} values")
    return "\n".join(lines) + "\n"


def change_log_csv(session: dict) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["rule", "column", "operation", "cells_changed", "rows_removed", "approval", "confidence"])
    for r in session["report"].get("change_log", []):
        w.writerow([_safe(r.get("rule")), _safe(r.get("column")), _safe(r.get("operation")),
                    r.get("cells_changed", 0), r.get("rows_removed", 0), _safe(r.get("approval")), r.get("confidence")])
    return out.getvalue()


def _safe(v):
    """Neutralise spreadsheet formula injection in exported text cells."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def quality_report_pdf(session: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    r = session["report"]
    h, comp = r["headline"], r["comparison"]
    brand = colors.HexColor("#201159")
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9.5, leading=13)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=brand, fontSize=13, spaceBefore=12, spaceAfter=6)
    title = ParagraphStyle("t", parent=ss["Title"], textColor=brand, fontSize=22, alignment=0)
    small = ParagraphStyle("s", parent=body, fontSize=8, textColor=colors.grey)

    def table(rows, widths, header=True):
        t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
        style = [("FONTSIZE", (0, 0), (-1, -1), 9), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                 ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d9d6e8")),
                 ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f5fb")])]
        if header:
            style += [("BACKGROUND", (0, 0), (-1, 0), brand), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold")]
        t.setStyle(TableStyle(style))
        return t

    def p(text):
        return Paragraph(escape(str(text)), body)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title=f"Omixa Data Quality Report - {session['dataset_name']}", author="Omixa")
    s = []
    s += [Paragraph("Data Quality Report", title), Paragraph("Omixa turns messy spreadsheets into consistent, professional documents.", small), Spacer(1, 8)]
    s.append(table([
        ["Dataset", p(session["dataset_name"])],
        ["Date processed", _when(session["created_at"])],
        ["Dataset size", f"{h['records']:,} records · {h['columns']} columns · {_size(session.get('file_size_bytes'))}"],
        ["Quality profile", p(session.get("profile_name") or "None (general analysis)")],
    ], [38 * mm, 136 * mm], header=False))

    s.append(Paragraph("Overall quality", h2))
    s.append(table([
        ["", "Before", "After"],
        ["Quality score", f"{comp['before']['score']}/100 ({comp['before']['grade']})", f"{comp['after']['score']}/100 ({comp['after']['grade']})"],
        ["Records", f"{comp['before']['rows']:,}", f"{comp['after']['rows']:,}"],
        ["Issues detected", comp["before"]["issues"], comp["after"]["issues"]],
        ["Duplicate records", comp["before"]["duplicates"], comp["after"]["duplicates"]],
        ["Missing values", comp["before"]["missing_values"], comp["after"]["missing_values"]],
        ["Validation failures", comp["before"]["validation_failures"], comp["after"]["validation_failures"]],
    ], [60 * mm, 57 * mm, 57 * mm]))

    s.append(Paragraph("Quality breakdown", h2))
    dims = [[d["label"], "n/a" if d["score"] is None else f"{d['score']}/100", d["issues"]] for d in r["dimensions"].values() if d.get("applicable", True)]
    s.append(table([["Dimension", "Score", "Issues"]] + dims, [80 * mm, 47 * mm, 47 * mm]))
    sev = r["severity"]
    s.append(Spacer(1, 4))
    s.append(p(f"Severity: {sev['critical']} critical · {sev['warning']} warning · {sev['information']} information. "
               f"{h['critical_structural_errors']} critical structural errors."))

    s.append(Paragraph("Issues detected (by column)", h2))
    rows = [["Column", "Score", "Issues"]]
    for c in [c for c in r["columns"] if c["issues"]][:25]:
        rows.append([p(c["column"]), f"{c['score']}/100", p("; ".join(f"{i['title']} ({i['count']}, {i['severity']})" for i in c["issues"][:3]))])
    s.append(table(rows, [45 * mm, 22 * mm, 107 * mm]) if len(rows) > 1 else p("No column-level issues remain."))

    s.append(Paragraph("Changes made", h2))
    for l in r["what_changed"]["lines"]:
        s.append(p(f"{'•' if l['status'] == 'ok' else '⚠'} {l['text']}".replace("⚠", "!")))

    s.append(Paragraph("Issues requiring review", h2))
    if r["review_items"]:
        s.append(table([["Issue", "Column", "Values", "Severity"]] + [[p(i["title"]), p(i.get("column") or "–"), i["count"], i["severity"]] for i in r["review_items"][:25]],
                       [70 * mm, 45 * mm, 25 * mm, 34 * mm]))
    else:
        s.append(p("Nothing requires manual review."))

    prof = r.get("profile")
    s.append(Paragraph("Validation results", h2))
    if prof:
        s.append(p(f"Profile: {prof['name']} · Compliance {prof['compliance_after']}% · {prof['passed']} passed, {prof['warnings']} warnings, {prof['failed']} failed"))
        s.append(Spacer(1, 4))
        s.append(table([["Rule", "Result", "Detail"]] + [[p(x["label"]), _STATUS_MARK[x["status"]], p(x["detail"])] for x in prof["results"]],
                       [55 * mm, 22 * mm, 97 * mm]))
    else:
        s.append(p("No Quality Profile was applied to this dataset."))
    s += [Spacer(1, 14), Paragraph(f"Generated by Omixa · {_when(session['created_at'])} · Figures only: this report contains no dataset values.", small)]
    doc.build(s)
    return buf.getvalue()
