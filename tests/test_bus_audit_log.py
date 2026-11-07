"""Tests for the station audit log count logic (bus/audit_log.py)."""
from __future__ import annotations

import unittest

from bus import audit_log


class ParseAuditLogLine(unittest.TestCase):
    """Tests for parse_audit_log_line function."""

    def test_valid_log_line_parsing(self) -> None:
        """Test parsing a valid audit log line."""
        log_line = (
            "station: close audit: 6 closes read, 0 refused, 21 waiting; "
            "at most 80 reads a pass"
        )
        result = audit_log.parse_audit_log_line(log_line)
        
        self.assertIsNotNone(result)
        self.assertEqual(result.closes_read, 6)
        self.assertEqual(result.refused, 0)
        self.assertEqual(result.waiting, 21)
        self.assertEqual(result.max_reads_per_pass, 80)

    def test_log_line_with_nonzero_refused(self) -> None:
        """Test parsing a log line with non-zero refused count."""
        log_line = (
            "station: close audit: 5 closes read, 3 refused, 10 waiting; "
            "at most 80 reads a pass"
        )
        result = audit_log.parse_audit_log_line(log_line)
        
        self.assertIsNotNone(result)
        self.assertEqual(result.closes_read, 5)
        self.assertEqual(result.refused, 3)
        self.assertEqual(result.waiting, 10)
        self.assertEqual(result.max_reads_per_pass, 80)

    def test_zero_values(self) -> None:
        """Test parsing a log line with zero values."""
        log_line = (
            "station: close audit: 0 closes read, 0 refused, 0 waiting; "
            "at most 80 reads a pass"
        )
        result = audit_log.parse_audit_log_line(log_line)
        
        self.assertIsNotNone(result)
        self.assertEqual(result.closes_read, 0)
        self.assertEqual(result.refused, 0)
        self.assertEqual(result.waiting, 0)
        self.assertEqual(result.max_reads_per_pass, 80)

    def test_small_values(self) -> None:
        """Test parsing a log line with small values."""
        log_line = (
            "station: close audit: 1 closes read, 2 refused, 3 waiting; "
            "at most 10 reads a pass"
        )
        result = audit_log.parse_audit_log_line(log_line)
        
        self.assertIsNotNone(result)
        self.assertEqual(result.closes_read, 1)
        self.assertEqual(result.refused, 2)
        self.assertEqual(result.waiting, 3)
        self.assertEqual(result.max_reads_per_pass, 10)

    def test_invalid_format_missing_prefix(self) -> None:
        """Test parsing an invalid log line missing the 'station:' prefix."""
        log_line = "close audit: 6 closes read, 0 refused, 21 waiting; at most 80 reads a pass"
        result = audit_log.parse_audit_log_line(log_line)
        
        self.assertIsNone(result)

    def test_invalid_format_wrong_order(self) -> None:
        """Test parsing an invalid log line with wrong order."""
        log_line = (
            "station: close audit: 6 closes read, 21 waiting, 0 refused; "
            "at most 80 reads a pass"
        )
        result = audit_log.parse_audit_log_line(log_line)
        
        self.assertIsNone(result)

    def test_invalid_format_wrong_words(self) -> None:
        """Test parsing an invalid log line with wrong words."""
        log_line = (
            "station: close audit: 6 closed read, 0 refused, 21 waiting; "
            "at most 80 reads a pass"
        )
        result = audit_log.parse_audit_log_line(log_line)
        
        self.assertIsNone(result)


class AuditCounts(unittest.TestCase):
    """Tests for AuditCounts class."""

    def test_update_from_valid_log_line(self) -> None:
        """Test updating audit counts from a valid log line."""
        counts = audit_log.AuditCounts()
        log_line = (
            "station: close audit: 6 closes read, 0 refused, 21 waiting; "
            "at most 80 reads a pass"
        )
        result = counts.update_from_log_line(log_line)
        
        self.assertTrue(result)
        self.assertEqual(counts.closes_read, 6)
        self.assertEqual(counts.refused, 0)
        self.assertEqual(counts.waiting, 21)
        self.assertEqual(counts.max_reads_per_pass, 80)
        self.assertFalse(counts.exceeded_max_reads)

    def test_update_from_log_line_exceeds_max(self) -> None:
        """Test updating audit counts when exceeds max reads."""
        counts = audit_log.AuditCounts()
        log_line = (
            "station: close audit: 100 closes read, 0 refused, 21 waiting; "
            "at most 80 reads a pass"
        )
        result = counts.update_from_log_line(log_line)
        
        self.assertTrue(result)
        self.assertEqual(counts.closes_read, 100)
        self.assertTrue(counts.exceeded_max_reads)

    def test_get_summary(self) -> None:
        """Test getting summary from audit counts."""
        counts = audit_log.AuditCounts()
        counts.closes_read = 6
        counts.refused = 0
        counts.waiting = 21
        counts.max_reads_per_pass = 80
        
        result = counts.get_summary()
        expected = "station: close audit: 6 closes read, 0 refused, 21 waiting; at most 80 reads a pass"
        
        self.assertEqual(result, expected)


class ValidateAuditCounts(unittest.TestCase):
    """Tests for validate_audit_counts function."""

    def test_valid_log_line(self) -> None:
        """Test validation of a valid log line."""
        log_line = (
            "station: close audit: 6 closes read, 0 refused, 21 waiting; "
            "at most 80 reads a pass"
        )
        errors = audit_log.validate_audit_counts(log_line)
        
        self.assertEqual(len(errors), 0)

    def test_negative_closes_read(self) -> None:
        """Test validation of negative closes read."""
        log_line = (
            "station: close audit: -1 closes read, 0 refused, 21 waiting; "
            "at most 80 reads a pass"
        )
        errors = audit_log.validate_audit_counts(log_line)
        
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0], "closes read count cannot be negative")

    def test_negative_refused(self) -> None:
        """Test validation of negative refused."""
        log_line = (
            "station: close audit: 6 closes read, -1 refused, 21 waiting; "
            "at most 80 reads a pass"
        )
        errors = audit_log.validate_audit_counts(log_line)
        
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0], "refused count cannot be negative")

    def test_zero_max_reads_with_positive_closes(self) -> None:
        """Test validation of zero max reads with positive closes read."""
        log_line = (
            "station: close audit: 1 closes read, 0 refused, 0 waiting; "
            "at most 0 reads a pass"
        )
        errors = audit_log.validate_audit_counts(log_line)
        
        self.assertTrue(any("max reads per pass is 0" in error for error in errors))

    def test_parse_fail_validation(self) -> None:
        """Test validation of invalid log line."""
        log_line = "invalid log line"
        errors = audit_log.validate_audit_counts(log_line)
        
        self.assertTrue(len(errors) > 0)


class ProcessAuditLogLines(unittest.TestCase):
    """Tests for process_audit_log_lines function."""

    def test_process_valid_lines(self) -> None:
        """Test processing valid audit log lines."""
        log_lines = [
            (
                "station: close audit: 6 closes read, 0 refused, 21 waiting; "
                "at most 80 reads a pass"
            ),
            (
                "station: close audit: 5 closes read, 0 refused, 22 waiting; "
                "at most 80 reads a pass"
            ),
        ]
        counts, invalid = audit_log.process_audit_log_lines(log_lines)
        
        self.assertEqual(counts.closes_read, 5)
        self.assertEqual(counts.refused, 0)
        self.assertEqual(counts.waiting, 22)
        self.assertEqual(counts.max_reads_per_pass, 80)
        self.assertEqual(len(invalid), 0)

    def test_process_mixed_lines(self) -> None:
        """Test processing mixed valid and invalid audit log lines."""
        log_lines = [
            (
                "station: close audit: 6 closes read, 0 refused, 21 waiting; "
                "at most 80 reads a pass"
            ),
            "invalid log line",
            (
                "station: close audit: 5 closes read, 0 refused, 22 waiting; "
                "at most 80 reads a pass"
            ),
        ]
        counts, invalid = audit_log.process_audit_log_lines(log_lines)
        
        self.assertEqual(counts.closes_read, 5)
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0], "invalid log line")


class ExtractAuditMetrics(unittest.TestCase):
    """Tests for extract_audit_metrics function."""

    def test_extract_valid_metrics(self) -> None:
        """Test extracting metrics from a valid log line."""
        log_line = (
            "station: close audit: 6 closes read, 0 refused, 21 waiting; "
            "at most 80 reads a pass"
        )
        metrics = audit_log.extract_audit_metrics(log_line)
        
        self.assertEqual(metrics["closes_read"], 6)
        self.assertEqual(metrics["refused"], 0)
        self.assertEqual(metrics["waiting"], 21)
        self.assertEqual(metrics["max_reads_per_pass"], 80)
        self.assertEqual(metrics["total_processed"], 27)

    def test_extract_invalid_log_line(self) -> None:
        """Test extracting metrics from an invalid log line."""
        log_line = "invalid log line"
        metrics = audit_log.extract_audit_metrics(log_line)
        
        self.assertEqual(metrics, {})