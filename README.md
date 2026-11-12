# READI – FHIR R5

FHIR **R5** material for the IHI READI project (*Building an Inclusive Clinical Studies
Ecosystem to Drive Health Equity in Europe*).

The R4 Implementation Guide is in [hl7-eu/readi](https://github.com/hl7-eu/readi)
([CI build](https://build.fhir.org/ig/hl7-eu/readi)).

For the time being this repository holds:

| Path | Purpose |
|------|---------|
| `examples/` | R5 test instances validated every day (see [examples/README.md](examples/README.md)). |
| `sources/` | `READI-sample-N.json`: R5 versions of the READI engine samples, produced by `python3 sources/update-readi-samples.py` (fetches the R4 Bundles served by the READI engine and converts them to R5; see the script header for the conversion rules). |
| `.github/workflows/validate-examples.yml` | Daily validation of `examples/` with the HL7 FHIR Java validator (FHIR 5.0.0). |
| `scripts/validation/` | Report script, baseline of accepted issues and suppressed messages. |
