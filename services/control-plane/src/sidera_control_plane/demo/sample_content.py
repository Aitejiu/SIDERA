from __future__ import annotations

from textwrap import dedent

OWNER_MARKER = "sidera-interview-demo-sample-v1\n"

CONTACTS_CSV = dedent(
    """\
    name,email,phone
    Alice,alice@example.com,13800138000
    Bob,bob@example.com,
    Bad Email,bob.example.com,13800138001
    Bad Phone,badphone@example.com,123
    """
)

CONTACTS_TSV = CONTACTS_CSV.replace(",", "\t")

INITIAL_VALIDATION = dedent(
    """\
    from __future__ import annotations

    def validate_rows(rows, *, phone_optional):
        raise NotImplementedError("validation stage has not run")
    """
)

INITIAL_REPORT = dedent(
    """\
    from __future__ import annotations

    def build_report(results):
        raise NotImplementedError("report stage has not run")
    """
)

INITIAL_IMPORTER = dedent(
    """\
    from __future__ import annotations

    def import_text(text, *, delimiter=",", phone_optional=True):
        raise NotImplementedError("integration stage has not run")
    """
)

IMPORTER_TESTS = dedent(
    """\
    import unittest
    from pathlib import Path

    from csv_importer.importer import import_text


    class ImporterTests(unittest.TestCase):
        def test_csv_import_reports_valid_and_invalid_rows(self):
            result = import_text(
                Path("fixtures/contacts.csv").read_text(encoding="utf-8"),
                phone_optional=True,
            )
            self.assertEqual(result["report"]["accepted"], 2)
            self.assertEqual(result["report"]["rejected"], 2)
            self.assertEqual(result["report"]["errors"][0]["row"]["name"], "Bad Email")


        def test_missing_phone_is_only_valid_when_optional(self):
            text = "name,email,phone\\nBob,bob@example.com,\\n"
            optional = import_text(text, phone_optional=True)
            required = import_text(text, phone_optional=False)
            self.assertEqual(optional["report"]["accepted"], 1)
            self.assertEqual(required["report"]["rejected"], 1)
    """
)

VALIDATION_SOURCE = dedent(
    """\
    from __future__ import annotations

    import re

    _EMAIL = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")
    _PHONE = re.compile(r"^1\\d{10}$")


    def validate_rows(rows, *, phone_optional):
        validated = []
        for index, row in enumerate(rows, start=1):
            normalized = {key: (value or "").strip() for key, value in row.items()}
            errors = []
            if not normalized.get("name"):
                errors.append("name_required")
            if not _EMAIL.fullmatch(normalized.get("email", "")):
                errors.append("email_invalid")
            phone = normalized.get("phone", "")
            if not phone and not phone_optional:
                errors.append("phone_required")
            elif phone and not _PHONE.fullmatch(phone):
                errors.append("phone_invalid")
            validated.append({"row": normalized, "row_number": index, "valid": not errors, "errors": errors})
        return validated
    """
)

REPORT_SOURCE = dedent(
    """\
    from __future__ import annotations


    def build_report(results):
        errors = [
            {"row": item["row"], "row_number": item["row_number"], "errors": item["errors"]}
            for item in results
            if not item["valid"]
        ]
        return {
            "accepted": sum(1 for item in results if item["valid"]),
            "rejected": sum(1 for item in results if not item["valid"]),
            "errors": errors,
        }
    """
)

INTEGRATION_SOURCE = dedent(
    """\
    from __future__ import annotations

    import csv
    import io

    from csv_importer.report import build_report
    from csv_importer.validation import validate_rows


    def import_text(text, *, delimiter=",", phone_optional=True):
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        rows = [dict(row) for row in reader]
        validation = validate_rows(rows, phone_optional=phone_optional)
        return {"validation": validation, "report": build_report(validation)}
    """
)

BROKEN_INTEGRATION_SOURCE = INTEGRATION_SOURCE.replace(
    "phone_optional=phone_optional", "phone_optional=False"
)

TSV_TESTS = dedent(
    """\
    import unittest
    from pathlib import Path

    from csv_importer.importer import import_text


    class TsvImporterTests(unittest.TestCase):
        def test_tsv_import_uses_the_revision_delimiter(self):
            result = import_text(
                Path("fixtures/contacts.tsv").read_text(encoding="utf-8"),
                delimiter="\\t",
                phone_optional=True,
            )
            self.assertEqual(result["report"]["accepted"], 2)
            self.assertEqual(result["report"]["rejected"], 2)
    """
)

REVIEW_ARTIFACT = dedent(
    """\
    # CSV import review

    The report stage keeps accepted and rejected row counts and retains row-level errors.
    """
)
