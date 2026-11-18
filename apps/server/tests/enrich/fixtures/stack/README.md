# Recorded source responses for the stack

What `KITTUM_GATEWAY_RECORDINGS` serves in the end-to-end journeys and `tests/app/test_enrichment.py`.
Recorded on 28.10.2026 by `uv run python -m tests.app.record_stack`, through Kittum's own HTTP
transport, with a contact address and no API key. `manifest.json` maps each
request URL, as the caller built it and without the contact address, to its body, status, and
content type. Where a source echoed the address in a body, the body holds
`contact@example.invalid` instead, as the gateway now writes it.

| Responses | Source and terms |
| --- | --- |
| `api.openalex.org` | OpenAlex works, CC0 |
| `api.crossref.org` | Crossref metadata, free to reuse; abstracts remain their publishers' |
| `www.ebi.ac.uk/europepmc` | Europe PMC search results; abstracts remain their publishers' |
| `api.unpaywall.org` | Unpaywall OA locations |
| `www.ncbi.nlm.nih.gov/research/pubtator3-api` | PubTator 3 annotations (NCBI public service); abstracts remain their publishers' |
| `europepmc.org`, `www.thelancet.com` | The HTML pages those hosts answered a PDF request with (HTTP 403) |
| `link.springer.com` | The two CC BY 4.0 trial PDFs, byte-identical to `tests/documents/fixtures/`, which the manifest references instead of copying |
