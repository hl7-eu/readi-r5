#!/usr/bin/env python3
"""Refresh sources/READI-sample-N.json (and its copy in examples/readi-engine/) with FHIR R5
versions of the READI engine samples.

Usage:
    python3 sources/update-readi-samples.py                 # all samples (1-10)
    python3 sources/update-readi-samples.py 1 4 8           # only these samples
    python3 sources/update-readi-samples.py --dry-run       # fetch, convert and report, write nothing
    python3 sources/update-readi-samples.py --keep-r4       # also save the R4 input as READI-sample-N-r4.json
    python3 sources/update-readi-samples.py --from-r4 DIR   # convert DIR/READI-sample-N.json instead of fetching

Adapted from sources/update-readi-samples.py in hl7-eu/readi (the R4 IG).

The READI engine only serves FHIR R4 Bundles conformant to the READI R4 IG
(http://hl7.eu/fhir/ig/readi): the API answers with a JSON-LD collection whose `member`
array holds a single Bundle. This script fetches it and converts it to FHIR R5:

- R5 cross-version extensions (http://hl7.org/fhir/5.0/StructureDefinition/extension-...)
  become the native R5 elements (label, name, descriptionSummary, studyDesign, classifier,
  outcomeMeasure, progressStatus, associatedParty, recruitment.targetNumber, EvidenceVariable.category);
- ResearchStudy: enrollment -> recruitment.eligibility, arm -> comparisonGroup,
  status (R4 study lifecycle) -> status `active` (R5 publication status) + progressStatus
  (only if the engine did not already provide a research-study-status progressStatus),
  sponsor / principalInvestigator -> associatedParty, category -> classifier,
  location -> region, focus -> CodeableReference, reasonStopped -> whyStopped,
  relatedArtifact -> relatesTo;
- Group: actual -> membership (enumerated | definitional), READI group-description -> description;
- EvidenceVariable: type -> handling, characteristic.definition[x] kept as in R5;
- Location / PractitionerRole: telecom -> contact.telecom (ExtendedContactDetail);
- READI R4 profiles are removed from meta.profile (there are no READI R5 profiles yet).

READI extensions with no R5 element (R6 pre-adoptions such as group-combinationMethod,
group-characteristic-description, group-characteristic-relativeTime, researchStudy-arm-eligibility,
researchStudy-arm-targetNumber, quantity-cdisc-unit) are kept unchanged.
Anything the script does not know how to convert is reported on stderr.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://readi-engine.io/api/public/clinical_trials/fhir/es"
SOURCES = Path(__file__).resolve().parent
EXAMPLES = SOURCES.parent / "examples" / "readi-engine"  # copy validated by the daily workflow

SAMPLES = {
    1: "NCT01444456",
    2: "NCT04077723",
    3: "NCT04710615",
    4: "NCT05841355",
    5: "NCT05160337",
    6: "NCT06422078",
    7: "NCT04436744",
    8: "2023-509661-21-00",
    9: "2024-512278-92-00",
    10: "NCT07203378",
}

READI = "http://hl7.eu/fhir/ig/readi/StructureDefinition/"
XVER = "http://hl7.org/fhir/5.0/StructureDefinition/extension-"
STUDY_STATUS = "http://hl7.org/fhir/research-study-status"
PARTY_ROLE = "http://hl7.org/fhir/research-study-party-role"

# R4 EvidenceVariable.type -> R5 EvidenceVariable.handling ("descriptive" has no R5 equivalent)
HANDLING = {"dichotomous": "dichotomous", "continuous": "continuous"}


class Log:
    def __init__(self):
        self.messages = []

    def __call__(self, msg):
        if msg not in self.messages:
            self.messages.append(msg)


# ---------------------------------------------------------------- fetch

def fetch(identifier):
    query = urllib.parse.urlencode({
        "page": 1,
        "itemsPerPage": 15,
        "entry.resource.identifier.value": identifier,
    })
    req = urllib.request.Request(f"{API}?{query}", headers={
        "Accept": "application/json",
        # the default Python-urllib agent is rejected (403) by the server
        "User-Agent": "curl/8.7.1",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    # application/json -> plain array; JSON-LD -> {"member": [...]}
    members = data if isinstance(data, list) else data.get("member")
    if not members:
        raise ValueError("empty response (no bundle returned)")
    if len(members) != 1:
        print(f"  warning: {len(members)} bundles returned, using the first", file=sys.stderr)
    return strip_jsonld(members[0])


def strip_jsonld(node):
    """Drop JSON-LD bookkeeping keys (@id, @type, @context) recursively."""
    if isinstance(node, dict):
        return {k: strip_jsonld(v) for k, v in node.items() if not k.startswith("@")}
    if isinstance(node, list):
        return [strip_jsonld(v) for v in node]
    return node


# ---------------------------------------------------------------- helpers

def sub(ext, name):
    """Values of the sub-extensions `name` of a complex extension, as (key, value) pairs."""
    out = []
    for e in ext.get("extension", []):
        if e.get("url") == name:
            out.extend((k, v) for k, v in e.items() if k.startswith("value") or k.startswith("_value"))
    return out


def sub_value(ext, name):
    vals = sub(ext, name)
    return vals[0][1] if vals else None


def put(target, key, value):
    if value not in (None, [], {}, ""):
        target[key] = value


def fix_meta(res):
    meta = res.get("meta")
    if not meta:
        return
    profiles = [p for p in meta.get("profile", []) if not p.startswith(READI)]
    if profiles:
        meta["profile"] = profiles
    else:
        meta.pop("profile", None)
    if not meta:
        res.pop("meta")


def telecom_to_contact(res):
    if "telecom" in res:
        res.setdefault("contact", []).append({"telecom": res.pop("telecom")})


def split_extensions(res, handlers, log, where):
    """Apply handlers[url](ext) to matching extensions; keep the others."""
    kept = []
    for ext in res.get("extension", []):
        url = ext.get("url", "")
        if url in handlers:
            handlers[url](ext)
        else:
            if url.startswith(XVER):
                log(f"{where}: no R5 conversion for {url}, kept as extension")
            kept.append(ext)
    if kept:
        res["extension"] = kept
    else:
        res.pop("extension", None)


# ---------------------------------------------------------------- ResearchStudy

def convert_research_study(rs, log):
    where = f"ResearchStudy/{rs.get('id')}"
    out = {}

    def ext_label(e):
        label = {}
        put(label, "type", sub_value(e, "type"))
        for k, v in sub(e, "value"):
            label[k.replace("valueString", "value")] = v
        out.setdefault("label", []).append(label)

    def ext_outcome(e):
        om = {}
        for name in ("name", "type", "description", "reference"):
            vals = [v for _, v in sub(e, name)]
            if name == "type":
                put(om, name, vals)
            elif vals:
                om[name] = vals[0]
        out.setdefault("outcomeMeasure", []).append(om)

    def ext_progress(e):
        ps = {}
        for name in ("state", "actual", "period"):
            put(ps, name, sub_value(e, name))
        out.setdefault("progressStatus", []).append(ps)

    def ext_party(e):
        party = {}
        for k, v in sub(e, "name"):
            party[k.replace("valueString", "name")] = v
        put(party, "role", sub_value(e, "role"))
        put(party, "period", [v for _, v in sub(e, "period")])
        put(party, "classifier", [v for _, v in sub(e, "classifier")])
        put(party, "party", sub_value(e, "party"))
        out.setdefault("associatedParty", []).append(party)

    handlers = {
        XVER + "ResearchStudy.label": ext_label,
        XVER + "ResearchStudy.name": lambda e: out.__setitem__("name", e["valueString"]),
        XVER + "ResearchStudy.descriptionSummary": lambda e: out.__setitem__("descriptionSummary", e["valueMarkdown"]),
        XVER + "ResearchStudy.studyDesign": lambda e: out.setdefault("studyDesign", []).append(e["valueCodeableConcept"]),
        XVER + "ResearchStudy.classifier": lambda e: out.setdefault("classifier", []).append(e["valueCodeableConcept"]),
        XVER + "ResearchStudy.outcomeMeasure": ext_outcome,
        XVER + "ResearchStudy.progressStatus": ext_progress,
        XVER + "ResearchStudy.associatedParty": ext_party,
        XVER + "ResearchStudy.recruitment.targetNumber":
            lambda e: out.setdefault("recruitment", {}).__setitem__("targetNumber", e["valueUnsignedInt"]),
    }
    split_extensions(rs, handlers, log, where)

    # R4 elements with a different name / type in R5
    r4_status = rs.pop("status", None)
    rs["status"] = "active"  # R5 status is the publication status of the record
    if r4_status:
        has_study_status = any(
            c.get("system") == STUDY_STATUS
            for ps in out.get("progressStatus", [])
            for c in ps.get("state", {}).get("coding", []))
        if not has_study_status:
            out.setdefault("progressStatus", []).append({
                "state": {"coding": [{"system": STUDY_STATUS, "code": r4_status}]},
                "actual": True,
            })

    enrollment = rs.pop("enrollment", [])
    if enrollment:
        out.setdefault("recruitment", {})["eligibility"] = enrollment[0]
        if len(enrollment) > 1:
            log(f"{where}: {len(enrollment)} enrollment Groups, only the first one kept as recruitment.eligibility")

    if "arm" in rs:
        groups = []
        for arm in rs.pop("arm"):
            cg = {k: arm[k] for k in ("id", "extension", "modifierExtension", "name", "type", "description") if k in arm}
            groups.append(cg)
        out["comparisonGroup"] = groups

    for r4, role in (("sponsor", "sponsor"), ("principalInvestigator", "primary-investigator")):
        if r4 in rs:
            out.setdefault("associatedParty", []).append({
                "role": {"coding": [{"system": PARTY_ROLE, "code": role}]},
                "party": rs.pop(r4),
            })
    if "category" in rs:
        out.setdefault("classifier", []).extend(rs.pop("category"))
    if "location" in rs:
        out["region"] = rs.pop("location")
    if "focus" in rs:
        out["focus"] = [{"concept": f} for f in rs.pop("focus")]
    if "reasonStopped" in rs:
        out["whyStopped"] = rs.pop("reasonStopped")
    if "relatedArtifact" in rs:
        out["relatesTo"] = rs.pop("relatedArtifact")
    for dropped in ("contact",):
        if dropped in rs:
            rs.pop(dropped)
            log(f"{where}: {dropped} has no R5 equivalent, dropped")

    rs.update(out)
    return order(rs, R5_RESEARCH_STUDY)


R5_RESEARCH_STUDY = [
    "resourceType", "id", "meta", "implicitRules", "language", "text", "contained", "extension",
    "modifierExtension", "url", "identifier", "version", "name", "title", "label", "protocol", "partOf",
    "relatesTo", "date", "status", "primaryPurposeType", "phase", "studyDesign", "focus", "condition",
    "keyword", "region", "descriptionSummary", "description", "period", "site", "note", "classifier",
    "associatedParty", "progressStatus", "whyStopped", "recruitment", "comparisonGroup", "objective",
    "outcomeMeasure", "result",
]


# ---------------------------------------------------------------- Group

def convert_group(g, log):
    where = f"Group/{g.get('id')}"

    def ext_description(e):
        if "description" in g:
            log(f"{where}: both description and group-description extension, extension dropped")
        else:
            g["description"] = e["valueMarkdown"]

    split_extensions(g, {READI + "group-description": ext_description}, log, where)
    if "actual" in g:
        g["membership"] = "enumerated" if g.pop("actual") else "definitional"
    g["contained"] = [convert_resource(c, log) for c in g.get("contained", [])] or None
    if g["contained"] is None:
        g.pop("contained")
    return order(g, R5_GROUP)


R5_GROUP = [
    "resourceType", "id", "meta", "implicitRules", "language", "text", "contained", "extension",
    "modifierExtension", "url", "identifier", "version", "name", "title", "status", "active", "type",
    "membership", "code", "description", "quantity", "managingEntity", "characteristic", "member",
]


# ---------------------------------------------------------------- EvidenceVariable

def convert_evidence_variable(ev, log):
    where = f"EvidenceVariable/{ev.get('id')}"

    def ext_category(e):
        cat = {}
        put(cat, "name", sub_value(e, "name"))
        for k, v in sub(e, "value"):
            cat[k] = v
        ev.setdefault("category", []).append(cat)

    split_extensions(ev, {XVER + "EvidenceVariable.category": ext_category}, log, where)
    if "type" in ev:
        t = ev.pop("type")
        if t in HANDLING:
            ev["handling"] = HANDLING[t]
        else:
            log(f"{where}: type '{t}' has no R5 handling code, dropped")
    for ch in ev.get("characteristic", []):
        for k in [k for k in ch if k.startswith("definition")]:
            if k in ("definitionDataRequirement", "definitionTriggerDefinition"):
                ch.pop(k)
                log(f"{where}: characteristic.{k} has no R5 equivalent, dropped")
        for k in ("usageContext", "participantEffectiveDescription", "participantEffectiveDateTime",
                  "participantEffectivePeriod", "participantEffectiveDuration", "participantEffectiveTiming",
                  "timeFromStart", "groupMeasure"):
            if k in ch:
                ch.pop(k)
                log(f"{where}: characteristic.{k} not converted to R5, dropped")
    return order(ev, R5_EVIDENCE_VARIABLE)


R5_EVIDENCE_VARIABLE = [
    "resourceType", "id", "meta", "implicitRules", "language", "text", "contained", "extension",
    "modifierExtension", "url", "identifier", "version", "versionAlgorithmString", "name", "title",
    "shortTitle", "status", "experimental", "date", "publisher", "contact", "description", "note",
    "useContext", "purpose", "copyright", "approvalDate", "lastReviewDate", "effectivePeriod", "author",
    "editor", "reviewer", "endorser", "relatedArtifact", "actual", "characteristicCombination",
    "characteristic", "handling", "category",
]


# ---------------------------------------------------------------- dispatch

def convert_resource(res, log):
    fix_meta(res)
    rt = res.get("resourceType")
    if rt == "ResearchStudy":
        return convert_research_study(res, log)
    if rt == "Group":
        return convert_group(res, log)
    if rt == "EvidenceVariable":
        return convert_evidence_variable(res, log)
    if rt in ("Location", "PractitionerRole"):
        telecom_to_contact(res)
        return res
    if rt != "Practitioner":
        log(f"{rt}/{res.get('id')}: no R5 conversion rules, copied as is")
    return res


def order(res, keys):
    """Return res with keys in R5 element order (unknown keys at the end)."""
    out = {k: res[k] for k in keys if k in res}
    out.update({k: v for k, v in res.items() if k not in out})
    return out


def convert_bundle(bundle, log):
    fix_meta(bundle)
    for entry in bundle.get("entry", []):
        entry["resource"] = convert_resource(entry["resource"], log)
    return bundle


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("numbers", nargs="*", type=int, help="sample numbers (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="do not write files")
    parser.add_argument("--keep-r4", action="store_true", help="also save the R4 input as READI-sample-N-r4.json")
    parser.add_argument("--from-r4", metavar="DIR", type=Path,
                        help="convert DIR/READI-sample-N.json (R4) instead of fetching from the READI engine")
    args = parser.parse_args()

    numbers = args.numbers or sorted(SAMPLES)
    unknown = [n for n in numbers if n not in SAMPLES]
    if unknown:
        parser.error(f"unknown sample number(s): {unknown}")

    failed = []
    for n in numbers:
        identifier = SAMPLES[n]
        target = SOURCES / f"READI-sample-{n}.json"
        print(f"[{n}] {identifier} -> {target.name}")
        try:
            if args.from_r4:
                r4 = json.loads((args.from_r4 / f"READI-sample-{n}.json").read_text(encoding="utf-8"))
            else:
                r4 = fetch(identifier)
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            failed.append(n)
            continue
        if args.keep_r4 and not args.dry_run:
            (SOURCES / f"READI-sample-{n}-r4.json").write_text(
                json.dumps(r4, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log = Log()
        bundle = convert_bundle(json.loads(json.dumps(r4)), log)
        print(f"  {len(bundle.get('entry', []))} entries")
        for msg in log.messages:
            print(f"  note: {msg}", file=sys.stderr)
        if not args.dry_run:
            text = json.dumps(bundle, indent=2, ensure_ascii=False) + "\n"
            target.write_text(text, encoding="utf-8")
            EXAMPLES.mkdir(parents=True, exist_ok=True)
            (EXAMPLES / target.name).write_text(text, encoding="utf-8")

    if failed:
        print(f"Failed: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
