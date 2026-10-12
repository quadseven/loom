"""Test the Finding class and related functions in bus.tickets."""
from bus.tickets import Finding, parse_findings, find_uncovered_dispositions, Fix, render_findings


def test_finding_creation():
    """Test creating a Finding object."""
    f = Finding("F1", "some text", location=("path.py", 10, 5))
    assert f.id == "F1"
    assert f.text == "some text"
    assert f.disposition is None
    assert f.location == ("path.py", 10, 5)


def test_finding_with_disposition():
    """Test creating a Finding with a disposition."""
    f = Finding("F1", "some text", disposition="existing_ticket", ticket="OWNER.NAME-123")
    assert f.disposition == "existing_ticket"
    assert f.ticket == "OWNER.NAME-123"


def test_finding_validation():
    """Test that Finding validates its inputs."""
    # Invalid ID
    try:
        Finding("X1", "text")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass
    
    # Invalid disposition
    try:
        Finding("F1", "text", disposition="invalid")
        assert False, "Should have raised ValueError"
    except ValueError:
        pass


def test_parse_findings_with_ids():
    """Test parsing findings with IDs."""
    body = """<!-- loom:fix v1 -->
Pull request: quadseven/loom#108, branch loom/108-test

### Thread F1 (comment 1) at test.py:10
Finding 1 text

### Thread F2 (comment 2) at test.py:20
Finding 2 text
"""
    findings = parse_findings(body)
    assert len(findings) == 2
    assert findings[0].id == "F1"
    assert findings[0].text == "Finding 1 text"
    assert findings[1].id == "F2"
    assert findings[1].text == "Finding 2 text"


def test_parse_findings_without_ids():
    """Test parsing findings without IDs."""
    body = """<!-- loom:fix v1 -->
Pull request: quadseven/loom#108, branch loom/108-test

### Thread (comment 1) at test.py:10
Finding 1 text

### Thread (comment 2) at test.py:20
Finding 2 text
"""
    findings = parse_findings(body)
    assert len(findings) == 2
    assert findings[0].id == "F1"
    assert findings[0].text == "Finding 1 text"
    assert findings[1].id == "F2"
    assert findings[1].text == "Finding 2 text"


def test_parse_findings_with_dispositions():
    """Test parsing findings with dispositions."""
    body = """<!-- loom:fix v1 -->
Pull request: quadseven/loom#108, branch loom/108-test

### Thread F1 (comment 1) at test.py:10
Finding 1 text
Fixing: OWNER.NAME-123

### Thread F2 (comment 2) at test.py:20
Finding 2 text
Does not apply
"""
    findings = parse_findings(body)
    assert len(findings) == 2
    assert findings[0].disposition == "existing_ticket"
    assert findings[0].ticket == "OWNER.NAME-123"
    assert findings[1].disposition == "does_not_apply"


def test_find_uncovered_dispositions():
    """Test finding findings without a disposition other than 'does_not_apply'."""
    findings = [
        Finding("F1", "text1", disposition="existing_ticket", ticket="T1"),
        Finding("F2", "text2", disposition="does_not_apply"),
        Finding("F3", "text3", disposition="fixed"),
    ]
    uncovered = find_uncovered_dispositions(findings)
    assert len(uncovered) == 1
    assert uncovered[0].id == "F2"


def test_fix_class_with_findings():
    """Test the Fix class with findings."""
    fix = Fix(108, "loom/108-test", """### Thread F1 (comment 1) at test.py:10
Finding 1 text
""")
    assert fix.pr == 108
    assert fix.branch == "loom/108-test"
    assert len(fix.findings_list) == 1
    assert fix.findings_list[0].id == "F1"


def test_fix_has_all_dispositions_false():
    """Test that Fix.has_all_dispositions returns False when findings have no dispositions."""
    fix = Fix(108, "loom/108-test", """### Thread F1 (comment 1) at test.py:10
Finding 1 text
""")
    assert not fix.has_all_dispositions()


def test_fix_has_all_dispositions_true():
    """Test that Fix.has_all_dispositions returns True when all findings have dispositions."""
    fix = Fix(108, "loom/108-test", """### Thread F1 (comment 1) at test.py:10
Finding 1 text
Does not apply
""")
    assert fix.has_all_dispositions()


def test_render_findings():
    """Test rendering findings with dispositions."""
    findings = [
        Finding("F1", "Finding 1 text", disposition="does_not_apply"),
        Finding("F2", "Finding 2 text", disposition="existing_ticket", ticket="OWNER.NAME-123"),
    ]
    rendered = render_findings(findings)
    assert "F1" in rendered
    assert "Finding 1 text" in rendered
    assert "F2" in rendered
    assert "Finding 2 text" in rendered
    assert "Fixing: OWNER.NAME-123" in rendered


def test_fix_findings_preserve_ids():
    """Test that finding IDs are preserved when parsing and re-rendering."""
    original_body = """<!-- loom:fix v1 -->
Pull request: quadseven/loom#108, branch loom/108-test

### Thread F1 (comment 1) at test.py:10
Finding 1 text
"""
    fix = Fix(108, "loom/108-test", original_body)
    # Re-parse and render
    findings = fix.findings_list
    rendered = render_findings(findings)
    assert "F1" in rendered
    assert "Finding 1 text" in rendered


def test_finding_serialization():
    """Test that Finding objects can be serialized and deserialized."""
    f1 = Finding("F1", "text", location=("path.py", 10, 5), disposition="existing_ticket", ticket="T1")
    data = f1.to_dict()
    f2 = Finding.from_dict(data)
    assert f1.id == f2.id
    assert f1.text == f2.text
    assert f1.disposition == f2.disposition
    assert f1.ticket == f2.ticket
    assert f1.location == f2.location
