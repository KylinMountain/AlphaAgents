"""Hand-labeled climactic bars for scanner recall test (spec §5.1).

Each entry: (code, date, climax_type, brief_visual_justification).
LABELED_BARS is read by test_vpa_v7_scanner_recall.py.

Anne Coulin's review §6 gap 1 mandates at least 3-5 labeled bars
covering both BC and SC. Add or revise this file as more historical
turning points are visually identified by the operator.
"""

LABELED_BARS = [
    # 300136 信维通信 — markup peak Jan 2026
    ("300136", "2026-01-23", "BC", "high $94.58 with close $93.50, vol 198M (60d top), wide range; followed by -10.1% on 1/26"),
    ("300136", "2026-01-26", "AR", "auto-reaction bar -10.1% with vol 186M after 1/23 climax"),
    # SC bars identified Apr 2026
    ("000559", "2026-04-10", "SC", "after -16.0% 30d drop from 19.44, vol 185M (2.34x avg), close in upper 36% of range, -1.0% next day"),
    ("000657", "2026-04-10", "SC", "after -18.6% 30d drop from 60.20, vol 90M (1.21x avg), close in upper 33% of range, -1.3% next day"),
]
