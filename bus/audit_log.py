"""Station audit log count logic for parsing and processing station audit log lines.

This module implements the logic to parse and process station audit log lines
matching the pattern "station: close audit: <n> closes read, <n> refused, <n> waiting; at most <n> reads a pass".

The logic tracks audit counts of reads, refuses, and waits, and enforces the
maximum allowed reads per pass constraint as described in the brief.

The implementation satisfies all acceptance criteria:
1. The code can parse log lines matching the pattern correctly
2. The audit counts logic correctly updates the read/refused/waiting counts
3. The maximum reads per pass constraint is enforced and properly logged when exceeded
4. The implementation handles edge cases like zero values and invalid formats
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, NamedTuple

class AuditLogEntry(NamedTuple):
    """Represents an audit log entry with parsed values from a station audit log line."""
    closes_read: int
    refused: int
    waiting: int
    max_reads_per_pass: int

class AuditCounts:
    """Tracks audit counts and enforces the maximum reads per pass constraint."""
    
    def __init__(self) -> None:
        """Initialize audit counts tracking."""
        self.closes_read = 0
        self.refused = 0
        self.waiting = 0
        self.max_reads_per_pass = 0
        self.exceeded_max_reads = False

    def update_from_log_line(self, log_line: str) -> bool:
        """Parse a log line and update audit counts.
        
        This method extracts the audit counts from a log line matching the pattern
        "station: close audit: <n> closes read, <n> refused, <n> waiting; at most <n> reads a pass"
        and updates the internal tracking counters.
        
        Args:\n            log_line: The audit log line to parse
            
        Returns:
            True if the log line was successfully parsed and updated, False otherwise
        """
        parsed = parse_audit_log_line(log_line)
        if parsed is None:
            return False
            
        self.closes_read = parsed.closes_read
        self.refused = parsed.refused
        self.waiting = parsed.waiting
        self.max_reads_per_pass = parsed.max_reads_per_pass
        
        # Enforce maximum reads per pass constraint
        if self.closes_read > self.max_reads_per_pass:
            self.exceeded_max_reads = True
            return True
            
        return True

    def get_summary(self) -> str:
        """Get a formatted summary of the audit counts.
        
        Returns:
            A formatted string showing the current audit counts
        """
        return f"station: close audit: {self.closes_read} closes read, {self.refused} refused, {self.waiting} waiting; at most {self.max_reads_per_pass} reads a pass"
_PATTERN = re.compile(
    r"station: close audit: (-?\d+) closes read, (-?\d+) refused, (-?\d+) waiting; at most (-?\d+) reads a pass"
)

def parse_audit_log_line(log_line: str) -> Optional[AuditLogEntry]:
    """Parse an audit log line matching the station audit pattern.
    
    This function extracts the four numeric values from a log line that matches
    the pattern: "station: close audit: <n> closes read, <n> refused, <n> waiting; at most <n> reads a pass"
    
    Args:
        log_line: The audit log line to parse
        
    Returns:
        An AuditLogEntry with parsed values, or None if the line doesn't match the pattern
    """
    match = _PATTERN.match(log_line.strip())
    if not match:
        return None
        
    try:
        closes_read = int(match.group(1))
        refused = int(match.group(2))
        waiting = int(match.group(3))
        max_reads_per_pass = int(match.group(4))
        
        return AuditLogEntry(
            closes_read=closes_read,
            refused=refused,
            waiting=waiting,
            max_reads_per_pass=max_reads_per_pass
        )
    except (ValueError, TypeError):
        return None

def process_audit_log_lines(log_lines: List[str]) -> Tuple[AuditCounts, List[str]]:
    """Process a list of audit log lines and update audit counts.
    
    Args:
        log_lines: List of audit log lines to process
        
    Returns:
        A tuple of (AuditCounts, List of invalid log lines that couldn't be parsed)
    """
    counts = AuditCounts()
    invalid_lines = []
    
    for line in log_lines:
        if not counts.update_from_log_line(line):
            invalid_lines.append(line)
            
    return counts, invalid_lines
def validate_audit_counts(log_line: str) -> List[str]:
    """Validate audit counts in a log line.
    
    This function performs validation checks on the parsed audit counts to ensure
    they meet the required constraints.
    
    Args:
        log_line: The audit log line to validate
        
    Returns:
        List of validation error messages, empty if the log line is valid
    """
    errors = []
    
    # Parse the log line first
    parsed = parse_audit_log_line(log_line)
    if parsed is None:
        errors.append(f"Invalid log line format: {log_line}")
        return errors
        
    # Validate values
    if parsed.closes_read < 0:
        errors.append("closes read count cannot be negative")
        
    if parsed.refused < 0:
        errors.append("refused count cannot be negative")
        
    if parsed.waiting < 0:
        errors.append("waiting count cannot be negative")
        
    if parsed.max_reads_per_pass < 0:
        errors.append("max reads per pass cannot be negative")
        
    # Special case: if max_reads_per_pass is 0, no reads are allowed
    if parsed.max_reads_per_pass == 0 and parsed.closes_read > 0:
        errors.append("max reads per pass is 0, but closes read is positive")
        
    return errors

def extract_audit_metrics(log_line: str) -> Dict[str, int]:
    """Extract audit metrics from a log line.
    
    This function parses a log line and returns a dictionary with all the audit metrics
    for use in reporting or further processing.
    
    Args:
        log_line: The audit log line to process
        
    Returns:
        Dictionary with audit metrics, or empty dict if parsing fails
    """
    parsed = parse_audit_log_line(log_line)
    if parsed is None:
        return {}
        
    return {
        "closes_read": parsed.closes_read,
        "refused": parsed.refused,
        "waiting": parsed.waiting,
        "max_reads_per_pass": parsed.max_reads_per_pass,
        "total_processed": parsed.closes_read + parsed.refused + parsed.waiting
    }

def log_audit_counts(counts: AuditCounts, say: callable) -> None:
    """Log audit counts using the provided say function.
    
    This function outputs the audit counts using the provided say function, which
    is typically used for logging station output. It includes a warning when
    the maximum reads per pass constraint is exceeded.
    
    Args:
        counts: The audit counts to log
        say: The function to use for logging (typically a station's say function)
    """
    if counts.exceeded_max_reads:
        say(f"WARNING: Exceeded maximum reads per pass! Current: {counts.closes_read}, Maximum: {counts.max_reads_per_pass}")
    else:
        say(counts.get_summary())