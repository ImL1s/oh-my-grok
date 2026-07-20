"""Private absolute-path report E2E was removed from the product suite.

Hermetic coverage lives in ``test_mock_report.py``.
For a real report path (optional, not collected by default CI):

  python tests/report_validator/validate.py /path/to/report.md
  OMG_RESEARCH_REPORT_PATH=/path/to/report.md python tests/report_validator/validate.py
"""
