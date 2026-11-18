"""Kittum: auditable, AI-assisted systematic literature reviews.

Each subpackage is one module from DESIGN.md. Import another module only through its package
(`from kittum.judge import Verdict`), never through its submodules. `tests/test_architecture.py`
enforces this and the allowed dependencies between modules.

The core is domain-neutral: nothing in it is specific to one agency, jurisdiction, or field.
"""
