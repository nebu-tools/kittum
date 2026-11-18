# Report test fixtures

- `nrt_embase.ris`: an Embase-style export for the nicotine replacement example. Two records
  duplicate CENTRAL records of `tests/screening/fixtures/nrt_central.ris` (same DOIs), one is a
  follow-up report of the CENTRAL patch trial (same NCT number), two are further trials. The
  records are illustrative, like the CENTRAL fixture.
- `tranexamic_medline.ris`, `tranexamic_central.ris`, `tranexamic_ctis.csv`: MEDLINE, CENTRAL,
  and CTIS exports for the tranexamic acid example (`examples/protocols/tranexamic-acid.yaml`).
  Everything in them is invented for the tests: trial names, authors, journals, abstracts, DOIs
  (under the `10.5555` test prefix), PMIDs, and trial numbers. The HARBOUR identifiers match the
  protocol's reference set.
