"""Placeholder phones, zero ages and abbreviation proposals (department-style columns)."""
import unittest

import pandas as pd

from cleaning.mapping import suggest_for_dataframe
from cleaning.checks import _placeholder_phone_mask


class PlaceholderPhones(unittest.TestCase):
    def test_repeated_run_and_flat_numbers_flagged(self):
        s = pd.Series(["0801234567", "0801234567", "0801234567", "+2348058537831", "08000000000", "+2348017507864"])
        m = _placeholder_phone_mask(s).tolist()
        self.assertEqual(m, [True, True, True, False, True, False])

    def test_real_distinct_numbers_not_flagged(self):
        s = pd.Series(["+2348058537831", "+2348017507864", "+2348078137358"])
        self.assertFalse(_placeholder_phone_mask(s).any())


class AbbreviationProposals(unittest.TestCase):
    def _dept(self, values):
        out = suggest_for_dataframe(pd.DataFrame({"department": values}))
        return out[0]["summary"] if out else None

    def test_ops_and_hr_proposed_for_approval_not_applied(self):
        sm = self._dept(["Operations", "Ops", "Human Resources", "HR", "Sales", "Operations", "Sales", "Human Resources"])
        self.assertEqual(sm["needs_approval"], 2)
        self.assertEqual(sm["will_apply"], 0)

    def test_distinct_cities_not_merged(self):
        self.assertIsNone(self._dept(["Abuja", "Aba", "Lagos", "Lagos", "Abuja"]))

    def test_ambiguous_short_form_left_alone(self):
        # "Ops" could abbreviate either label, so nothing is proposed.
        self.assertIsNone(self._dept(["Operations", "Opposites", "Ops", "Operations", "Opposites"]))


if __name__ == "__main__":
    unittest.main()


class LocalPhoneFormat(unittest.TestCase):
    def test_local_vs_international(self):
        from cleaning.phone_formats import normalize_phone as n
        self.assertEqual(n("+2348058537831", "NG"), "+2348058537831")
        self.assertEqual(n("+2348058537831", "NG:local"), "08058537831")
        self.assertEqual(n("8058537831", "NG:local"), "08058537831")
        self.assertEqual(n("+44 7911 123456", "NG:local"), "+447911123456")   # foreign number untouched
        self.assertIsNone(n("0801234567", "NG:local"))                         # not a valid Nigerian length

    def test_option_accepts_local_suffix_and_rejects_junk(self):
        from cleaning.options import OptionsError, parse_options
        self.assertEqual(parse_options({"phone_country": "ng:local"})["phone_country"], "NG:local")
        with self.assertRaises(OptionsError):
            parse_options({"phone_country": "NG:weird"})

    def test_resolution_options_offer_both_forms(self):
        from cleaning.resolutions import RESOLUTION_OPTIONS
        ids = {o["id"] for o in RESOLUTION_OPTIONS["suspicious_phone_format"]}
        self.assertTrue({"NG", "NG:local"} <= ids)


class ImpossibleAgesRemoved(unittest.TestCase):
    def test_blank_invalid_removes_negative_and_over_120(self):
        from cleaning.resolutions import apply_resolutions
        df = pd.DataFrame({"age": [20, -4, 130, 0, 57]})
        out, log = apply_resolutions(df, [{"column": "age", "issue": "impossible_age", "choice": "blank_invalid"}])
        self.assertEqual(out["age"].isna().tolist(), [False, True, True, False, False])
