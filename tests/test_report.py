"""Tests for HTML report rendering."""

from mutest.report import render_report


def _ctx():
    return {
        "target": "demo",
        "version": "0.1.0",
        "score": 0.75,
        "total": 4, "killed": 3, "survived": 1,
        "timeout": 1, "error": 1, "elapsed_s": 12.3,
        "files": [{"name": "calc.py", "total": 4, "killed": 3,
                   "survived": 0, "timeout": 1, "error": 0, "score": 0.75}],
        "survived_mutants": [{
            "id": "calc.py:14:ROR:1", "operator": "ROR",
            "description": ">= -> >",
            "original_snippet": "    return age >= 18",
            "mutated_snippet": "    return age > 18",
        }],
        "generated_tests": [{
            "name": "test_mutest_boundary", "code": "def test_mutest_boundary():\n    assert True\n",
            "target": "calc.py:14:ROR:1", "model": "fake", "status": "killed ✓",
        }],
    }


def test_report_renders(tmp_path):
    html = render_report(_ctx())
    assert "75.0%" in html
    assert "calc.py:14:ROR:1" in html
    assert "&gt;=" in html or ">=" in html  # snippet present (escaped or not)
    assert "test_mutest_boundary" in html
    assert "mutest 0.1.0" in html
    assert "timeout (not killed)" in html
    assert "errors (not killed)" in html


def test_report_escapes_untrusted_values_and_shows_unresolved_statuses():
    ctx = _ctx()
    ctx["target"] = "<script>alert('target')</script>"
    ctx["unresolved_mutants"] = [{
        "id": "m.py:1:ERR:1", "operator": "ERR",
        "description": "<script>alert('description')</script>",
        "original_snippet": "return '<script>'",
        "mutated_snippet": "return '<script>mutated</script>'",
        "status": "timeout", "detail": "pytest timed out <now>",
    }]
    html = render_report(ctx)
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html
    assert "timeout" in html


def test_report_no_survivors_message():
    ctx = _ctx()
    ctx["survived"] = 0
    ctx["survived_mutants"] = []
    ctx["killed"] = 4
    ctx["timeout"] = 0
    ctx["error"] = 0
    ctx["score"] = 1.0
    html = render_report(ctx)
    assert "No survived mutants" in html


def test_report_without_ai_section():
    ctx = _ctx()
    ctx["generated_tests"] = []
    html = render_report(ctx)
    assert "AI-generated tests" not in html
