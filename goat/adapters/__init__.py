"""Per-ATS adapters: the application URL to open, a deterministic prefill of the mechanical fields
(name, contact, education) from the answer store, portal hints for the model, and a few portal
mechanics written as code (Greenhouse embed URL, Workday rail, Lever location autocomplete).

The model handles only what the adapter cannot. Adapters never submit."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from .. import answers, records
from ..browser import fill
from ..browser.snapshot import Snapshot

# label regex -> (answer key, kind). kind: text | choose
GENERIC_FIELD_MAP: list[tuple[re.Pattern, str, str]] = [
    # one box for the whole name comes before the first-name rule (Hadrian's "First Name and Last Name", 2026-10-18)
    (re.compile(r"^(first (name )?(and|&) last name|first and last name|name \(first and last\)|full legal name)\b", re.I), "identity.legal_name", "text"),
    (re.compile(r"^(legal )?first ?name\b|^given name", re.I), "identity.first_name", "text"),
    (re.compile(r"^(legal )?last ?name\b|^surname|^family name", re.I), "identity.last_name", "text"),
    # a preferred name box that wants both names takes the full name (Sierra's "Preferred First & Last Name" got
    # only "Surya", 2026-10-19)
    (re.compile(r"^preferred (first (name )?(and|&) last name|full name|name \(first and last\))\b", re.I), "identity.legal_name", "text"),
    (re.compile(r"^preferred (first )?name", re.I), "identity.preferred_name", "text"),
    (re.compile(r"^(full|legal) name$|^name$", re.I), "identity.legal_name", "text"),
    (re.compile(r"^e-?mail( address)?\b", re.I), "identity.email", "text"),
    # never "Phone Extension" / "Phone Ext." (Ryan's Workday form got the whole number there, 2026-10-19)
    (re.compile(r"^(?!.*\bext(ension)?\b)((mobile |cell |primary )?phone( number)?\b|telephone)", re.I), "identity.phone", "text"),
    (re.compile(r"^(street )?address( line)? ?1?$|^address$|^street", re.I), "identity.address_line", "text"),
    (re.compile(r"city,? state,? (and |& )?zip", re.I), "identity.city_state_zip", "text"),
    # IXL (2026-10-19): "Your Location (Search by City)" was typed, not picked, and Greenhouse refused the submit twice
    (re.compile(r"^(your |current )?(home )?location( \([^)]*\))?\s*\*?$|^(current )?city of residence|^where are you (currently )?(located|based)\b", re.I),
     "identity.location_city", "choose"),
    (re.compile(r"^city\b", re.I), "identity.city", "text"),
    (re.compile(r"^(zip|postal)( code)?\b", re.I), "identity.zip", "text"),
    (re.compile(r"^linkedin( profile| url)?\b", re.I), "identity.linkedin", "text"),
    (re.compile(r"^github( profile| url)?\b", re.I), "identity.github", "text"),
    (re.compile(r"^(website|portfolio|personal site)( url)?\b", re.I), "identity.website", "text"),
    (re.compile(r"^(school|university|college|institution)( name)?\b", re.I), "education.school", "choose"),
    (re.compile(r"^(major|field of study|discipline|area of study)\b", re.I), "education.major", "choose"),
    (re.compile(r"^(gpa|grade point average)\b", re.I), "education.gpa", "text"),
    (re.compile(r"^(state|state/province|state or province)\b", re.I), "identity.state", "choose"),
    (re.compile(r"^country\b", re.I), "identity.country", "choose"),
    (re.compile(r"^(expected |anticipated )?graduation (date|year|month)?\b", re.I), "education.graduation", "text"),
]

# Consents, acknowledgements and attestations Surya gives on every application (his instruction,
# 2026-10-15: sign and consent on his behalf). Marketing, newsletters and talent communities stay off.
CONSENT = re.compile(
    r"(\bi (have read|agree|acknowledge|consent|certify|understand|accept|authori[sz]e|attest|confirm)|terms (and|&) conditions|"
    r"terms of (use|service)|privacy (policy|notice|statement)|data (processing|privacy|protection)|\bgdpr\b|\bconsent|acknowledg|"
    r"\bcertif|\battest|\bauthori[sz]|background (check|screening|investigation)|drug (test|screen)|e-?signature|"
    r"electronic(ally)? sign|true and (complete|accurate|correct)|anti-nepotism|voluntary disclosure)", re.I)
MARKETING = re.compile(
    r"(marketing|newsletter|promotion|job alerts?|talent (community|network|pool)|future (job )?opportunit|similar (jobs|roles|positions)|"
    r"updates about (new )?(jobs|roles|opportunities)|new job opportunities|keep me (informed|updated)|subscribe|"
    r"share my (information|data|profile) with|third[- ]part(y|ies) (marketing|partners)|recruiting events?)", re.I)
SMS = re.compile(r"(\bsms\b|text messag|\btexts?\b about)", re.I)
# Surya (2026-10-15): consent to processing his information, creating an account or applying is standard on every
# application: always consent, even when the same box also mentions receiving information or communications.
PROCESSING = re.compile(r"(process|processing|collect|store|storing|use of (my|your) (personal )?(information|data)|personal (information|data)|"
                        r"privacy|terms|account|to apply|application|positions with|recruit(ing|ment) purposes|consider (me|my))", re.I)
SIGNATURE_LABEL = re.compile(r"(e-?signature|electronic signature|^signature\b|^sign (here|your name)|type your (full )?(legal )?name|"
                             r"signature \(type|applicant signature|full name as signature)", re.I)
SIGNATURE_DATE_LABEL = re.compile(r"^(today'?s date|date signed|signature date|date of signature|current date|date)\s*\*?$", re.I)


# "Would you be willing to disclose, post-offer and as necessary, information related to your political contributions
# as required by SEC Rule 206(4)-5?" (TIAA, 2026-10-19): compliance with a rule or policy is a consent he delegated.
COMPLIANCE_DISCLOSURE = re.compile(
    r"\bwilling to (disclose|comply|provide|submit|undergo|report)\b.{0,200}\b(required|requirement|rules?|regulations?|polic(y|ies)|laws?|sec)\b",
    re.I | re.S)


# Keeping his profile or data for OTHER or FUTURE openings is a Yes: his questionnaire (2026-10-18) answers "May the
# company keep his profile and consider him for future openings? Yes (a plain marketing / newsletter opt-in is still
# No)" (consent.keep_profile_future_roles). Aptiv's "Do you consent for your data to be further processed for other job
# opportunities?" (2026-10-19) is that question. Newsletters, job alerts and marketing stay No (MARKETING).
FUTURE_ROLES = re.compile(
    r"(\bfor (other|future|additional|new|further) (job |career |employment )?(opportunit|openings?|positions?|roles?|vacanc)|"
    r"talent (pool|database)|kept on file for|future (job )?consideration|keep (my|your|his) (profile|information|data|resume|résumé|cv))",
    re.I)
# Job alerts by any wording: Berkshire Hathaway Energy's Oracle box "I agree to receive updates about new job projects." was
# ticked as his keep-my-profile consent (the judge caught it, 2026-10-22).
JOB_ALERTS = re.compile(
    r"(job alerts?|\b(receive|get|send me)\b.{0,30}\b(updates|alerts|notifications|news)\b.{0,30}\b(new )?(jobs?|job projects?|positions?|"
    r"roles?|opportunit\w*|openings?|vacanc\w*)|\bupdates about (new |future |similar )?(jobs?|job projects?|positions?|roles?|"
    r"opportunit\w*|openings?))", re.I)
NEWSLETTER = re.compile(r"(newsletter|job alerts?|marketing|promotion|subscribe|text messag|\bsms\b|e-?mails? about|"
                        + JOB_ALERTS.pattern + ")", re.I)


def future_roles_consent(label: str) -> bool:
    return bool(FUTURE_ROLES.search(label or "")) and not NEWSLETTER.search(label or "")


# Joining a community or network is his never-join rule, whatever else the box says: LLNL's "...agree to be part of
# communities for future career opportunities" was ticked as a future-roles consent (2026-10-21).
TALENT_COMMUNITY = re.compile(r"\b((talent|career|candidate)s?\s+(communit(y|ies)|network|club)|communit(y|ies)\s+for\s+(future|other)|"
                              r"part of (our |the )?communit(y|ies)|join (our |the )?(talent|career|candidate))\b", re.I)


def consent_key(label: str) -> str | None:
    """The standing answer key that authorizes ticking this consent, or None to leave it alone."""
    if TALENT_COMMUNITY.search(label or ""):
        return "policy.may_join_talent_community"
    if SMS.search(label):
        return "consent.sms"
    if future_roles_consent(label):
        return "consent.keep_profile_future_roles"
    if COMPLIANCE_DISCLOSURE.search(label):
        return "consent.compliance_disclosure"
    if re.search(r"(authori[sz]ed to work|work authori[sz]ation|eligib(le|ility) to work|without restrictions?|sponsorship|legally (able|permitted))", label, re.I):
        return None                       # a work-authorization question, not a consent (Niantic, judged 2026-10-18)
    if MARKETING.search(label) and not PROCESSING.search(label):
        return None                       # a purely optional marketing / newsletter / talent-community opt-in
    if not CONSENT.search(label):
        return None
    if re.search(r"background (check|screening|investigation)|consumer report", label, re.I):
        return "compliance.background_check_consent"
    if re.search(r"drug (test|screen)", label, re.I):
        return "compliance.drug_test_consent"
    if re.search(r"anti-nepotism", label, re.I):
        return "consent.anti_nepotism_ack"
    if re.search(r"voluntary disclosure|self-identification", label, re.I):
        return "consent.voluntary_disclosure"
    if re.search(r"(certif|attest|true and (complete|accurate|correct)|truthful|accurate and complete|signature)", label, re.I):
        return "consent.truthfulness_attestation"
    return "consent.privacy_notice"


# "Which available location(s) on the posting are you open to relocating to? Please only select locations that are
# listed on the posting. Type city and states below (e.g. Palmdale, CA)." (Northrop Grumman, 2026-10-19): the box wants
# the posting's own locations, not his "Anywhere in the United States".
POSTING_LOCATIONS = re.compile(
    r"(locations?(\(s\))?\s+(that are |which are )?(listed |shown |offered |posted )?(on|in) (the|this) (job )?(posting|requisition|job description|job ad)\b|"
    r"(available|listed|posted|offered) locations?(\(s\))? (on|in|for) (the|this) (job|posting|position|role|requisition)\b)", re.I)
LOCATION_KEY = re.compile(r"(relocat|location|office)", re.I)


def posting_locations(job: dict | None) -> list[str]:
    """The listing's own "City, ST" locations (Remote and region-only entries left out)."""
    import json as _json
    raw = (job or {}).get("locations")
    try:
        locs = _json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except Exception:
        locs = [x for x in str(raw or "").split(";")]
    out = []
    for x in locs:
        x = str(x or "").strip()
        if x and "," in x and not re.search(r"\bremote\b", x, re.I) and x not in out:
            out.append(x)
    return out


SELF_ID = re.compile(r"(gender|race|ethnicit|hispanic|veteran|disabilit|pronoun|transgender|sexual orientation)", re.I)
NEGATED = re.compile(r"^(not |no |none|i do not|i am not|unwilling|unable|never|decline|prefer not)", re.I)


SELF_ID_DECLINE = re.compile(r"\b(decline|don'?t wish|do not wish|not wish to|prefer not|rather not|choose not|not to (say|answer|disclose)|"
                             r"undisclosed|not specified|not declared|do not want to answer|self[- ]identify)\b", re.I)


def leading_group(parts: list[str]) -> list[str]:
    """The leading entries of an ordered self-identification chain that answer the same way as its first entry (all
    declines, or all his own answer)."""
    parts = [x.strip() for x in parts if x and x.strip()]
    if not parts:
        return []
    first = bool(SELF_ID_DECLINE.search(parts[0]))
    out = []
    for x in parts:
        if bool(SELF_ID_DECLINE.search(x)) != first:
            break
        out.append(x)
    return out


def _standing_values() -> dict[str, str]:
    """normalized answer value -> key, for approved global answers with distinctive values (>= 4 chars,
    not a bare yes/no), so a prefilled field equal to a stored answer can be sourced without retyping."""
    from .. import db
    out: dict[str, str] = {}
    for r in db.connect().execute("SELECT key, answer FROM answers WHERE superseded_by IS NULL AND approved=1 AND answer IS NOT NULL "
                                  "AND scope='global' AND key NOT LIKE 'story.%' AND key NOT LIKE 'policy.%' AND key NOT LIKE 'hermes.generated.%'"):
        parts = str(r["answer"]).split(";")
        if str(r["key"]).startswith("self_id."):
            # A self-identification chain is ordered: its leading wordings are his answer and the rest are fallbacks for
            # forms that lack them. "Asian" (the race fallback) shown by SmartRecruiters from an earlier application was
            # taken as sourced although the list offered "Prefer not to answer" (CapTech, 2026-10-21).
            parts = leading_group(parts)
        for part in parts:
            nv = answers.norm(part)
            if len(nv) >= 4 and nv not in ("yes", "no", "none", "true", "false") and nv not in out:
                out[nv] = r["key"]
    return out


# A file box for the site's job matching, not the application: Texas Instruments' Oracle search page offered "Upload or
# drag and drop your resume here to get recommended jobs based on your skills and experience." (2026-10-20). The prefill
# attached the résumé there, Oracle parsed it into his candidate profile, and the application then carried two education
# tiles he never entered ('Computer Science', 'Engineering Honors'), each with a required field to fix.
JOB_MATCH_UPLOAD = re.compile(r"recommended (jobs|roles|positions|opportunities)|job recommendations|recommend(ed)? jobs|"
                              r"job alerts?|matching jobs|jobs (that )?match|get matched|match(ed)? (you )?(to|with) jobs|"
                              r"find (your )?(next )?(jobs?|roles?)|search jobs|based on your (skills|resume|résumé|experience)", re.I)


def is_job_match_upload(label: str, url: str = "", portal: str = "") -> bool:
    """A résumé box that feeds the site's job matching rather than this application."""
    if JOB_MATCH_UPLOAD.search(label or ""):
        return True
    # Oracle's application lives under /apply/: a résumé box elsewhere (the search page) is the matching one
    return (portal or "").lower() == "oracle" and "/apply" not in (url or "") and bool(url)


# Oracle's "Import your profile from resume" box parses the résumé into the form: Berkshire Hathaway Energy (2026-10-21)
# got seven broken Work and Education History entries ('Unnamed Job Title', 'Engineering Honors' with no dates), Oracle
# said "You have 7 issues that need to be fixed before you can submit", and SUBMIT did nothing. The résumé goes into the
# Supporting Documents slot instead.
PROFILE_IMPORT_UPLOAD = re.compile(r"\bimport\b|\bautofill\b|\bauto-fill\b|\bparse\b", re.I)


def is_profile_import_upload(label: str, portal: str = "") -> bool:
    """An Oracle file box that imports the résumé into the form's fields rather than attaching it."""
    return (portal or "").lower() == "oracle" and bool(PROFILE_IMPORT_UPLOAD.search(label or ""))


def resume_path_for(job: dict):
    """The résumé file for this job's variant, from delegation.yaml (never a file on the never_attach list)."""
    import yaml
    from ..config import SETTINGS
    deleg = yaml.safe_load((SETTINGS.config_dir / "delegation.yaml").read_text(encoding="utf-8")) or {}
    res = deleg.get("resumes") or {}
    variant = job.get("resume_variant") or "swe"
    name = (res.get("by_variant") or {}).get(variant) or (res.get("by_variant") or {}).get("general") or "Surya_Thirukonda_Resume_SWE.pdf"
    if name in (res.get("never_attach") or []):
        return None
    return SETTINGS.resume_dir / name


@dataclass
class PrefillResult:
    set: int = 0
    failed: int = 0
    skipped_self_id: int = 0
    details: list[dict] | None = None


class Adapter:
    portal = "other"
    lane = "no_account"
    hints = ""

    def apply_url(self, listing_url: str) -> str:
        return listing_url

    def opening_note(self, attempt: int) -> str:
        base = "Begin: call snapshot, then fill this page. The mechanical fields may already be prefilled by the harness; verify them in the outline."
        return base + ("\n" + self.hints if self.hints else "")

    def is_review_page(self, snap: Snapshot) -> bool:
        return any(re.search(r"\breview\b", h, re.I) for h in snap.headings)

    def field_map(self) -> list[tuple[re.Pattern, str, str]]:
        return GENERIC_FIELD_MAP

    async def _run_chooses(self, ex, chooses, job: dict, page_no: int, res) -> None:
        for ref, label, key, value in chooses:
            wants = [w.strip() for w in value.split(";") if w.strip()]
            variants = answers.get(key + "_variants")
            if variants and variants.answer:
                wants += [w.strip() for w in str(variants.answer).split(";")]
            fallbacks = answers.get(key.rsplit(".", 1)[0] + ".major_fallbacks") if key.endswith(".major") else None
            if fallbacks and fallbacks.answer:
                wants += [w.strip() for w in str(fallbacks.answer).split(";")]
            r = await fill.choose(ex, ref, wants)
            if r.ok:
                res.set += 1
                records.record_decision(job["id"], page_no, label, r.readback, f"standing:{key}", ref)
            else:
                res.failed += 1
            res.details.append({"label": label, "ok": r.ok, "error": r.error})

    async def _recheck_typed(self, ex, plan: list[dict], job: dict, page_no: int, res) -> None:
        lost = []
        for p in plan:
            try:
                info = await ex.read_ref(p["ref"])
            except Exception:
                continue
            if info.get("found") and answers.norm(info.get("value") or "") != answers.norm(p["value"]):
                lost.append(p)
        if not lost:
            return
        out = await fill.fill_batch(ex, [{"ref": p["ref"], "value": p["value"]} for p in lost])
        by = {r.ref: r for r in out}
        for p in lost:
            r = by.get(p["ref"])
            ok = bool(r and r.ok)
            if ok:
                records.record_decision(job["id"], page_no, p["_label"], r.readback, f"standing:{p['_key']}", p["ref"])
            res.details.append({"label": p["_label"], "ok": ok, "retyped": True,
                                "error": "" if ok else "a later field cleared it and re-typing failed"})

    async def prefill(self, ex, snap: Snapshot, job: dict, page_no: int) -> PrefillResult:
        """Fill empty plain fields whose label matches the map. Never touches self-identification,
        attestations, password fields or anything already holding a value."""
        res = PrefillResult(details=[])
        plan, chooses = [], []
        # Pass 0: verify fields that already hold a value (a previous attempt, browser or portal prefill).
        # A value equal to a stored answer is sourced deterministically; anything else is left for the model
        # to re-set or clear, and the gate will refuse it until then.
        store = _standing_values()
        for item in snap.items:
            if item.get("tag") not in ("input", "textarea", "select", "dropdown") or item.get("type") in ("checkbox", "radio", "file", "password"):
                continue
            val = (item.get("shown") or item.get("value") or "").strip()
            label = item.get("label") or ""
            if not val or not label:
                continue
            nv = answers.norm(val)
            key = None
            if item.get("tag") in ("input", "textarea") and POSTING_LOCATIONS.search(label) and posting_locations(job):
                # a box for the posting's own locations holds them all (Northrop Grumman held "Anywhere in the United
                # States", 2026-10-19): verified when it does, re-typed when it does not
                want = "; ".join(posting_locations(job))
                if nv == answers.norm(want):
                    records.record_decision(job["id"], page_no, label, val, "standing:availability.relocation_scope", item["ref"])
                    res.set += 1
                    res.details.append({"label": label, "ok": True, "verified": "posting locations"})
                else:
                    plan.append({"ref": item["ref"], "value": want, "_label": label, "_key": "availability.relocation_scope"})
                continue
            for pat, k, _kind in self.field_map():
                if pat.search(label.strip().rstrip("*").strip()):
                    a = answers.get(k, job.get("company", ""), self.portal)
                    if a and a.has_value:
                        na = answers.norm(str(a.answer))
                        if nv == na or (len(na) >= 6 and nv.startswith(na)) or any(nv == answers.norm(x) for x in str(a.answer).split(";")):
                            key = k
                    break
            if key is None:
                key = store.get(nv)
            if key:
                records.record_decision(job["id"], page_no, label, val, f"standing:{key}", item["ref"])
                res.set += 1
                res.details.append({"label": label, "ok": True, "verified": key})
        # Pressed toggle buttons (Ashby Yes/No) whose question resolves to a stored answer are sourced too.
        for item in snap.items:
            if item.get("tag") != "toggle" or not item.get("pressed"):
                continue
            q = item.get("group") or ""
            a = answers.lookup(q, job.get("company", ""), self.portal) if q else None
            if not a or not a.has_value:
                continue
            ans = answers.norm(str(a.answer)); lab = answers.norm(item.get("label") or "")
            ok = (lab == ans) or (ans in ("yes", "true") and lab == "yes") or (ans == "no" and lab == "no")
            if ok:
                records.record_decision(job["id"], page_no, f"{q[:80]} / {item.get('label', '')[:30]}", "pressed", f"standing:{a.key}", item["ref"])
                res.set += 1
                res.details.append({"label": f"{q[:40]} / {item.get('label', '')}", "ok": True, "verified": a.key})
        # Checked radio/checkbox options whose group resolves to a stored answer are sourced the same way.
        for g in snap.groups:
            glabel = g.get("label") or ""
            a = answers.lookup(glabel, job.get("company", ""), self.portal) if glabel else None
            if not a or not a.has_value:
                continue
            ans = answers.norm(str(a.answer))
            for o in g.get("options", []):
                if not o.get("checked"):
                    continue
                ol = (o.get("label") or "").strip()
                nol = answers.norm(ol)
                negated = bool(NEGATED.match(ol))
                ok = (nol == ans) or (ans in ("yes", "true") and not negated) or (ans == "no" and negated)
                if ok:
                    records.record_decision(job["id"], page_no, f"{glabel[:80]} / {ol[:40]}", "checked", f"standing:{a.key}", o["ref"])
                    res.set += 1
                    res.details.append({"label": f"{glabel[:40]} / {ol[:30]}", "ok": True, "verified": a.key})
        await self._consents(ex, snap, job, page_no, res)
        for item in snap.items:
            label = item.get("label") or ""
            if not label or item.get("disabled"):
                continue
            if SELF_ID.search(label):
                res.skipped_self_id += 1
                continue
            if item.get("tag") not in ("input", "textarea", "select", "dropdown") or item.get("type") in ("checkbox", "radio", "file", "password"):
                continue
            if item.get("value") or item.get("shown"):
                continue
            if item.get("tag") in ("input", "textarea") and POSTING_LOCATIONS.search(label) and posting_locations(job):
                plan.append({"ref": item["ref"], "value": "; ".join(posting_locations(job)), "_label": label,
                             "_key": "availability.relocation_scope"})
                continue
            for pat, key, kind in self.field_map():
                if pat.search(label.strip().rstrip("*").strip()):
                    a = answers.get(key, job.get("company", ""), self.portal)
                    if not a or not a.has_value:
                        break
                    if item.get("tag") in ("dropdown", "select") or item.get("combo") or kind == "choose" and item.get("tag") != "input":
                        chooses.append((item["ref"], label, key, str(a.answer)))
                    else:
                        plan.append({"ref": item["ref"], "value": str(a.answer), "_label": label, "_key": key})
                    break
        # Résumé: attach deterministically when an empty Resume/CV file input exists (never the cover letter slot);
        # a slot that already shows the expected file is verified and sourced without re-uploading.
        for item in snap.items:
            if item.get("type") != "file":
                continue
            label = " ".join([(item.get("label") or ""), (item.get("placeholder") or ""), (item.get("name") or "")])
            if re.search(r"cover|transcript", label, re.I) or not re.search(r"resume|résumé|\bcv\b|curriculum", label, re.I):
                continue
            if is_job_match_upload(label, getattr(ex, "url", "") or "", self.portal):
                res.details.append({"label": item.get("label"), "ok": True, "skipped": "a job-matching upload, not the application"})
                continue
            if is_profile_import_upload(label, self.portal):
                res.details.append({"label": item.get("label"), "ok": True, "skipped": "a profile import, not the résumé slot"})
                continue
            path = resume_path_for(job)
            if path is None or not path.exists():
                break
            if item.get("value"):
                if item["value"].lower() == path.name.lower():
                    records.record_decision(job["id"], page_no, item.get("label") or "resume upload", path.name, "resume", item["ref"])
                    job["resume_file"] = path.name
                    res.set += 1
                    res.details.append({"label": item.get("label"), "ok": True, "verified": "resume already attached"})
                else:
                    res.details.append({"label": item.get("label"), "ok": False, "error": f"slot holds {item['value']}, expected {path.name}"})
                break
            r = await fill.upload_file(ex, item["ref"], path)
            if r.ok:
                res.set += 1
                records.record_decision(job["id"], page_no, item.get("label") or "resume upload", path.name, "resume", item["ref"])
                job["resume_file"] = path.name
            else:
                res.failed += 1
            res.details.append({"label": item.get("label"), "ok": r.ok, "error": r.error})
            break
        # Country before anything typed: Greenhouse clears a typed phone number when the country changes.
        first = [c for c in chooses if "country" in c[2]]
        chooses = first + [c for c in chooses if c not in first]
        n_first = len(first)
        await self._run_chooses(ex, chooses[:n_first], job, page_no, res)
        if plan:
            out = await fill.fill_batch(ex, [{"ref": p["ref"], "value": p["value"]} for p in plan])
            by = {r.ref: r for r in out}
            for p in plan:
                r = by.get(p["ref"])
                if r and r.ok:
                    res.set += 1
                    records.record_decision(job["id"], page_no, p["_label"], r.readback, f"standing:{p['_key']}", p["ref"])
                else:
                    res.failed += 1
                res.details.append({"label": p["_label"], "ok": bool(r and r.ok), "error": r.error if r else "no result"})
        await self._run_chooses(ex, chooses[n_first:], job, page_no, res)
        # A later step (a picker, a résumé re-parse) can clear what was typed: check and re-type once.
        await self._recheck_typed(ex, plan, job, page_no, res)
        return res


SELECT_ALL_BOX = re.compile(r"^\s*(select|check|tick|accept|agree to)\s+all\b", re.I)


async def _consents_impl(self, ex, snap: Snapshot, job: dict, page_no: int, res: PrefillResult) -> None:
    """Tick required consents/acknowledgements/attestations, type the e-signature and today's date.
    Each one is recorded with the standing key that authorizes it; anything unclear is left to the model."""
    done: set[str] = set()
    company = job.get("company", "")

    def ok_key(key: str | None) -> bool:
        if not key:
            return False
        a = answers.get(key, company, self.portal)
        return bool(a and a.has_value and answers.norm(str(a.answer)).split(" ")[0] in ("yes", "true", "i", "accept", "agree"))

    ticks = []
    for item in snap.items:
        if item.get("type") != "checkbox" or item.get("checked") or item.get("disabled"):
            continue
        # A 'Select all' box ticks every box under it, the optional ones too: LLNL's took 'join future career communities'
        # (his never-join rule) along with the privacy notices, and unticking it cleared the required ones (2026-10-21).
        # Each required box is ticked on its own instead.
        if SELECT_ALL_BOX.match(item.get("label") or ""):
            continue
        label = " ".join([item.get("label") or "", item.get("group") or ""]).strip()
        key = consent_key(label)
        if ok_key(key) and not NEGATED.match((item.get("label") or "").strip()):
            ticks.append((item["ref"], (item.get("label") or label)[:120], key))
            done.add(item["ref"])
    for g in snap.groups:
        glabel = g.get("label") or ""
        opts = g.get("options", [])
        if not glabel or any(o.get("checked") for o in opts):
            continue
        key = consent_key(glabel)
        if not ok_key(key):
            continue
        yes = [o for o in opts if o.get("ref") not in done and not NEGATED.match((o.get("label") or "").strip())
               and re.match(r"^(yes|i (agree|accept|acknowledge|consent|certify|understand|authori[sz]e|confirm)|agree|accept|acknowledged?)\b",
                            (o.get("label") or "").strip(), re.I)]
        if len(yes) == 1 and (len(opts) <= 3):
            ticks.append((yes[0]["ref"], f"{glabel[:90]} / {(yes[0].get('label') or '')[:30]}", key))
            done.add(yes[0]["ref"])
    # A marketing / job-alert opt-in left ticked (a default, or an earlier attempt) is unticked: his standing No
    # (onsemi's 'I agree to receive updates about new job opportunities.' stayed ticked on Oracle, 2026-10-19).
    mk = answers.get("consent.marketing_email", company, self.portal)
    if mk and mk.has_value and answers.norm(str(mk.answer)).split(" ")[0] in ("no", "false", "decline"):
        unticks = [i for i in snap.items if i.get("type") == "checkbox" and i.get("checked") and not i.get("disabled")
                   and ((MARKETING.search(i.get("label") or "") and not PROCESSING.search(i.get("label") or "")
                         and not future_roles_consent(i.get("label") or "")
                         and not SMS.search(i.get("label") or ""))
                        or TALENT_COMMUNITY.search(i.get("label") or ""))]
        if unticks:
            out = await fill.fill_batch(ex, [{"ref": i["ref"], "value": "false"} for i in unticks])
            for i, r in zip(unticks, out):
                if r.ok:
                    res.set += 1
                    records.record_decision(job["id"], page_no, (i.get("label") or "")[:120], "unchecked",
                                            "standing:consent.marketing_email", i["ref"])
                res.details.append({"label": (i.get("label") or "")[:120], "ok": r.ok, "consent": "consent.marketing_email",
                                    "unticked": True, "error": r.error if not r.ok else ""})
    if ticks:
        out = await fill.fill_batch(ex, [{"ref": r, "value": "true"} for r, _, _ in ticks])
        by = {r.ref: r for r in out}
        for ref, label, key in ticks:
            r = by.get(ref)
            if r and r.ok:
                res.set += 1
                records.record_decision(job["id"], page_no, label, "checked", f"standing:{key}", ref)
            else:
                res.failed += 1
            res.details.append({"label": label, "ok": bool(r and r.ok), "consent": key, "error": r.error if r and not r.ok else ""})
    # Electronic signature: his full legal name, and today's date beside it.
    sig_refs = []
    for item in snap.items:
        label = (item.get("label") or "").strip().rstrip("*").strip()
        if item.get("tag") not in ("input", "textarea") or item.get("type") in ("checkbox", "radio", "file", "password"):
            continue
        if SIGNATURE_LABEL.search(label) and not (item.get("value") or "").strip():
            sig_refs.append((item["ref"], label))
    if sig_refs:
        a = answers.get("bank.signature", company, self.portal) or answers.get("identity.legal_name", company, self.portal)
        if a and a.has_value:
            out = await fill.fill_batch(ex, [{"ref": r, "value": str(a.answer)} for r, _ in sig_refs])
            for (ref, label), r in zip(sig_refs, out):
                if r.ok:
                    res.set += 1
                    records.record_decision(job["id"], page_no, label, r.readback, f"standing:{a.key}", ref)
                res.details.append({"label": label, "ok": r.ok, "signature": True, "error": r.error})
    has_signature = bool(sig_refs) or any(SIGNATURE_LABEL.search(i.get("label") or "") for i in snap.items)
    da = answers.get("consent.signature_date", company, self.portal)
    if has_signature and da and da.has_value:
        today, _ = answers.concrete_value(da)
        for item in snap.items:
            label = (item.get("label") or "").strip()
            if item.get("tag") != "input" or item.get("type") in ("checkbox", "radio", "file", "password") or (item.get("value") or "").strip():
                continue
            if SIGNATURE_DATE_LABEL.match(label):
                r = await fill.set_date(ex, item["ref"], today)
                if r.ok:
                    res.set += 1
                    records.record_decision(job["id"], page_no, label, r.readback or today, "standing:consent.signature_date", item["ref"])
                res.details.append({"label": label, "ok": r.ok, "signature_date": today, "error": r.error})


Adapter._consents = _consents_impl


class Greenhouse(Adapter):
    portal = "greenhouse"
    hints = ("Greenhouse: dropdowns are react-select (use choose; never press Enter in one). Short answers are often capped at 255 "
             "characters. The 'protected by reCAPTCHA' badge is not a challenge. If the page asks for an emailed security code "
             "after submit, call enter_email_code with the code box refs, then submit again. School on Greenhouse is "
             "'Texas A&M University - College Station'. An Employment section (Company name / Title / Start date / End date / Current role, "
             "'Add another') is filled by the harness when the page opens and whenever you call fill_sections: every experience.work.N entry; "
             "never add or edit those blocks by hand, only what fill_sections reports failed.")

    ENTRY_LABEL = re.compile(r"^(company name|title|start date( month| year)?|end date( month| year)?|current role|add another|remove employment|toggle flyout)\b", re.I)

    async def prefill(self, ex, snap: Snapshot, job: dict, page_no: int) -> PrefillResult:
        from .greenhouse_sections import GreenhouseSections, is_employment_page
        if is_employment_page(snap):
            rep = await GreenhouseSections(ex, job, page_no).run()
            from .. import db
            db.event(job["id"], "adapter:sections", rep.summary())
            fresh = await _resnap(ex)
            snap = Snapshot(url=fresh.url, title=fresh.title, headings=fresh.headings,
                            items=[i for i in fresh.items if not self.ENTRY_LABEL.match((i.get("label") or "").strip())],
                            groups=[g for g in fresh.groups if "current role" not in (g.get("label") or "").lower()],
                            errors=fresh.errors, challenge=fresh.challenge)
            res = await super().prefill(ex, snap, job, page_no)
            res.set += len(rep.added) + len(rep.updated)
            res.failed += len(rep.failed)
            (res.details or []).append({"label": "Employment section", "ok": not rep.failed, "sections": rep.summary()})
            return res
        return await super().prefill(ex, snap, job, page_no)

    def apply_url(self, listing_url: str) -> str:
        u = urlparse(listing_url)
        host = u.netloc.lower()
        q = parse_qs(u.query)
        if "gh_jid" in q and "greenhouse.io" not in host:
            board = host.split(".")[-2] if host.count(".") >= 1 else host
            if host.startswith(("careers.", "jobs.", "www.")):
                board = host.split(".")[1]
            # Wrappers that carry the board token in the path: careerpuck (Lyft) /job-board/<board>/job/<id>
            mb = re.search(r"/job-board/([^/]+)/", u.path)
            if mb:
                board = mb.group(1)
            return f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={q['gh_jid'][0]}"
        m = re.match(r"^/([^/]+)/jobs/(\d+)", u.path)
        if "greenhouse.io" in host and m and "/embed/" not in u.path:
            return f"https://job-boards.greenhouse.io/embed/job_app?for={m.group(1)}&token={m.group(2)}"
        return listing_url


class Ashby(Adapter):
    portal = "ashby"
    hints = ("Ashby: short single-page forms; fields are virtualized so take a fresh snapshot after scrolling. Uploading the résumé "
             "can trigger 'Autofill from resume': read the form back afterwards and correct anything that differs from the stored answers. "
             "Location is 'Sugar Land, TX'. 'Education History' (School, Field of Study, End Date, 'Still Student?') is filled by the "
             "harness when the page opens: his school, his major, 'Still Student?' ticked and the End Date left empty (he is a current "
             "student; never pick an end month or year there). Leave that block as it is.")

    async def prefill(self, ex, snap: Snapshot, job: dict, page_no: int) -> PrefillResult:
        res = await super().prefill(ex, snap, job, page_no)
        from .ashby_education import fill_education
        try:
            rep = await fill_education(ex, job, page_no)
        except Exception as e:  # noqa: BLE001 - a failed helper leaves the block to the agent
            rep = {"set": 0, "failed": 1, "kept": 0, "errors": [f"{type(e).__name__}: {str(e)[:160]}"]}
        if rep is not None:
            from .. import db
            db.event(job["id"], "adapter:education", rep)
            res.set += rep.get("set", 0)
            res.failed += rep.get("failed", 0)
            (res.details if res.details is not None else []).append({"label": "Education History", "ok": not rep.get("failed"), **rep})
        return res

    def apply_url(self, listing_url: str) -> str:
        u = listing_url.rstrip("/")
        return u if u.endswith("/application") else u + "/application"


class Lever(Adapter):
    portal = "lever"
    lane = "ready_for_you"
    hints = ("Lever: single page, résumé upload at the top (it may overwrite name and contact fields: read back after uploading). "
             "'Current location' is an autocomplete: use choose with 'Sugar Land, TX, USA' and confirm the error line 'No location found' "
             "is gone. Lever shows an hCaptcha at submit on nearly every posting: fill everything, then call park with "
             "'ready_for_you: hCaptcha then Submit'.")

    def apply_url(self, listing_url: str) -> str:
        u = listing_url.rstrip("/")
        return u if u.endswith("/apply") else u + "/apply"


class Workday(Adapter):
    portal = "workday"
    lane = "account"
    hints = ("Workday: you start on the posting page. Dismiss any cookie banner (Decline or Accept is fine), click 'Apply', then "
             "'Apply Manually' if a choice is offered (never 'Use My Last Application' or 'Autofill with Resume'). A Sign In / Create "
             "Account page follows. Order: (1) on the Sign In form call credentials mode 'login_universal' and click Sign In; "
             "(2) if the click reports sign_in 'failed' (wrong email/password, account locked) this tenant holds an older password: click "
             "'Forgot your password?', fill the email from identity.email, send, call open_password_reset_link, snapshot, credentials mode "
             "'set_password' (confirm box via confirm_password_ref), click save, then credentials login_universal and Sign In again; "
             "(3) if Sign In says no account exists, switch to Create Account, call credentials mode 'create' with confirm_password_ref, tick the "
             "required checkbox, click Create Account, then open_verification_link (or enter_email_code for a code); "
             "(4) if open_password_reset_link reports no reset email, the tenant has no account for this email: do (3), Create Account; "
             "park 'credentials_unknown' only when Create Account says the account already exists. "
             "Then a multi-page rail (My Information, My Experience, Application Questions, Voluntary Disclosures, Self Identify, Review). "
             "Every page's primary button is 'Save and Continue' EXCEPT the last, where the same button says 'Submit': never click it; call submit. "
             "'Select One' buttons and prompt boxes are custom "
             "dropdowns: use choose (State -> Texas; Phone Device Type -> Mobile; Country -> United States of America; Field of Study -> "
             "Computer Science; How Did You Hear -> Job Board then LinkedIn; its list shows categories and choose searches inside them, so a "
             "'LinkedIn' chip means it is picked even when no top-level row says LinkedIn). The school is 'Texas A & M University'. Date widgets: use choose "
             "or the calendar; a typed year may not commit. 'Enter website' marked for robots only is a honeypot: leave it empty. "
             "My Experience: the harness fills Work Experience (every experience.work.N entry with its full description), Websites and Skills itself when the page "
             "opens, and again whenever you call fill_sections; read its report, then do only what it lists as failed plus Education (school prompt, "
             "degree, field of study, dates) and the résumé. Never add a work entry by hand while fill_sections has not been called on this page. "
             "Education holds his Texas A&M entry (education.school, degree, major, GPA 4.0, 2025 to May 2028); add a high school "
             "entry only when the form requires one, and a high school entry never gets a Field of Study (GlobalFoundries, "
             "2026-10-20: 'Computer and Information Science' was typed under Clements High School). On a return visit Workday keeps, "
             "on Save and Continue, only the Education entries this visit added or changed: to fix one entry there, delete every "
             "Education entry and add each of them again (GlobalFoundries lost its Texas A&M entry when only the high school one "
             "was edited). "
             "Languages (when the section is there and its Language list offers English; when it does not, leave the section empty): English, tick 'I am fluent in this language' (answer_key experience.fluent_in_english), and each "
             "proficiency dropdown from experience.english_proficiency; the Review page must not show 'I am fluent in this language: No'. "
             "A resumed application opens at its last step: go_to_step('My Experience'), call fill_sections, then Save and Continue forward to Review. "
             "Date fields (graduation, employment dates) are three spinbuttons that ignore ordinary typing: always use "
             "`set_date` (graduation = education.graduation_mmddyyyy, i.e. 05/15/2028). Revio IS a current role: tick "
             "'I currently work here' and leave its end date blank. Certifications are optional: add his AWS Certified Cloud "
             "Practitioner only when the Certification list offers it; if it does not (Axiom Space, Northrop Grumman list only "
             "other AWS exams), delete that certification block and move on, never pick another certificate. Then a Review "
             "showing 'Certifications: No Response' is correct and final: never go back to My Experience for it.")

    def is_review_page(self, snap: Snapshot) -> bool:
        return any(re.search(r"\breview\b", h, re.I) for h in snap.headings) or "review" in snap.url.lower()

    ENTRY_LABEL = re.compile(r"^(job title|company|location|month|year|day|calendar|role description|description|url|website|"
                             r"i currently work here|delete|add( another)?)\b", re.I)

    async def prefill(self, ex, snap: Snapshot, job: dict, page_no: int) -> PrefillResult:
        """My Experience: the deterministic section filler adds every work entry and website first; the generic
        pass then skips the entry fields (its 'Location' rule would otherwise type his home city into a job's location)."""
        from .workday_sections import WorkdaySections, is_experience_page
        if is_experience_page("", snap.headings) or any(re.match(r"^(job title|company)\*?$", i.get("label") or "", re.I) for i in snap.items):
            rep = await WorkdaySections(ex, job, page_no).run()
            from .. import db
            db.event(job["id"], "adapter:sections", rep.summary())
            fresh = await _resnap(ex)
            snap = Snapshot(url=fresh.url, title=fresh.title, headings=fresh.headings,
                            items=[i for i in fresh.items if not self.ENTRY_LABEL.match((i.get("label") or "").strip())],
                            groups=[g for g in fresh.groups if "currently work here" not in (g.get("label") or "").lower()],
                            errors=fresh.errors, challenge=fresh.challenge)
            res = await super().prefill(ex, snap, job, page_no)
            res.set += len(rep.added) + len(rep.updated)
            res.failed += len(rep.failed)
            (res.details or []).append({"label": "My Experience sections", "ok": not rep.failed, "sections": rep.summary()})
            return res
        return await super().prefill(ex, snap, job, page_no)


async def _resnap(ex) -> Snapshot:
    from ..browser import snapshot as snapmod
    return await snapmod.take(ex)


class Oracle(Adapter):
    portal = "oracle"
    lane = "account"
    hints = ("Oracle Recruiting Cloud: an account or an emailed access code is required; use credentials / enter_email_code. "
             "After enter_email_code, click the VERIFY / Next button yourself (it is an ordinary button, not a human check), "
             "then fill the application form that opens. "
             "Sessions time out after 30 idle minutes. Oracle uses role=alert for status chatter such as 'The form Address form loaded'; "
             "the harness already ignores that. Fill the Address block completely: a missing home address blocked a submit before. "
             "One long page (CSX, 2026-10-19): Yes/No and band questions are toggle buttons (fill_batch with the answer_key: GPA band "
             "education.gpa_range, travel share availability.travel_max_percent); 'How did you hear' is a list where LinkedIn may be "
             "missing: pick 'Other' and type 'LinkedIn' in its 'Specify Other' box; 'ADD EXPERIENCE' opens one block per role "
             "(experience.work.N title, company, dates) and 'ADD EDUCATION' one for Texas A&M University (major, graduation May 2028; "
             "when the School search offers no Texas A&M University, pick 'Other', his own rule, and type the name in an 'Other' "
             "box if one appears); "
             "Veteran Status is a dropdown (self_id.veteran_status); the Full Name signature takes his legal name. Work through every "
             "section top to bottom before calling submit; never hand the form back while a stored answer covers what is left. "
             "Experience and Education keep each saved entry as a tile with Edit / Delete (onsemi, 2026-10-19): read the tiles "
             "first and add only stored entries that have no tile, never a second copy. 'Add Experience' opens a form: Employer "
             "Name, Job Title, Employer City / Country, then the Start Date and End Date pickers (Month and Year are separate "
             "pickers: choose the month name, then the year), Current Job for the current role, Responsibilities from "
             ".description; then click the form's own ADD EXPERIENCE (or ADD EDUCATION) button to save it as a tile. A form "
             "left open for an entry that already has a tile is closed with its CANCEL button. The resume import can add "
             "tiles that are not his entries (GM Financial, 2026-10-19: an 'Engineering Honors' education record with a "
             "required Degree): delete any tile that matches none of his stored entries with its Delete button, then go on. "
             "The outline lists each tile as ENTRY TILE with its own Edit and Delete refs ('Edit: <title> / <employer>'); a tile "
             "from an earlier résumé import can name a skill list as its employer (AutoZone, 2026-10-21): open it with its Edit, "
             "set Employer Name and the other fields from his stored entry for that role, and SAVE.")


class ICIMS(Adapter):
    portal = "icims"
    lane = "account"
    hints = ("iCIMS: account creation with an emailed six-digit code (enter_email_code). The form runs in an iframe; take a snapshot after each step.")


class SmartRecruiters(Adapter):
    portal = "smartrecruiters"
    lane = "no_account"
    # Wellmark (2026-10-21): the résumé upload parsed project names ('Tempo | Node.js, WebSockets, ...') into Experience cards
    # with missing or wrong dates, the form then said "Please provide at least one work experience entry" / "Please provide
    # start date", and the run parked over the parsed cards instead of fixing them.
    # CapTech (2026-10-21): every button is a bare <button> in a custom element's shadow root; the snapshot now names them
    # by their host, and the harness writes the Experience / Education cards itself (smartrecruiters_sections).
    hints = ("SmartRecruiters one-click form: the harness writes the Experience and Education cards from his stored entries "
             "whenever you take a snapshot (the outline's HARNESS line says what it did): never add, edit or delete those "
             "cards yourself, and never park over them. If the HARNESS line reports a failure, call fill_sections once, then "
             "go on. Upload the résumé into the Resume box lower on the page, never the 'autocomplete your application' box "
             "at the top (it rewrites Experience wrongly). The harness also picks City ('Sugar Land, TX, US'): never type into "
             "City (typed text is dropped and the pick is lost), and leave the phone Country code as it is (the number "
             "carries +1). If City is empty, call fill_sections. Fill the rest of the page, click Next, answer the "
             "screening questions from stored answers, then call submit.")


class SuccessFactors(Adapter):
    portal = "successfactors"
    lane = "account"
    # Westinghouse and L3Harris (2026-10-21): the 'No Selection' dropdowns show a pick only after the box loses focus, a
    # 'Please complete your personal information.' message lingers after the fields are set, and the last button is 'Apply'.
    hints = ("SAP SuccessFactors (career*.successfactors.com, *.ns2cloud.com 'portalcareer'): one long form. Its dropdowns are "
             "'No Selection' boxes: set each with choose and the answer_key (the box shows the pick once it loses focus; the "
             "harness checks after that). A 'Please complete your personal information.' message clears by itself once the "
             "fields are set: never park over it. Values the portal kept from his earlier SuccessFactors profile stand when they "
             "match his stored answers; re-set the ones that differ (work history from experience.work.N.*, never project "
             "names). Salutation is identity.salutation (Mr). The final button is labelled 'Apply': when the page is complete "
             "call submit with button_label 'Apply'. On its Create Account page the password rules under 'Choose Password' "
             "('Password must be at least 8 characters long ...') are always shown and are not a rejection: his password "
             "meets them. Fill every required account field (Retype Email Address, First Name, Last Name, Country/Region "
             "Code, Phone Number, Country/Region of Residence) from the stored answers, set both password boxes with "
             "credentials mode 'create', then click Create Account (Acuity and Corning parked over that text, 2026-10-21). "
             "Its 'Terms of Use' / 'Data Privacy Acknowledgement' box is a link ('Read and accept the data privacy statement.', "
             "'Please read our Candidate Privacy Notice'), not a dropdown: click it, then click the statement's own Accept / "
             "I agree / I accept button; accepting it is delegated to you (Corning went ready_for_you trying choose on it, "
             "2026-10-21). If Create Account answers 'Invalid password' under the rules, park credentials_unknown quoting "
             "the rule it shows (Phillips 66 asks for 12 characters). "
             "Uploading the résumé can make SuccessFactors parse it into Employment History / Formal Education rows with "
             "wrong values (Corning, 2026-10-21: skill lists and 'UNSPECIFIED' as Company Name, the Physio hackathon as a job, "
             "a Southwest row with no Position Title, a duplicate Founding Software Engineer). Those rows are yours to fix, "
             "never a reason to park: match each row to his stored entry by its dates and set Position Title and Company "
             "Name (and Work Location, Current Role, dates) from experience.work.N.*; click Remove on a row that is none of "
             "his roles (a project, a duplicate, 'UNSPECIFIED' with no stored match); set School to education.school, Degree "
             "and Major from education.*; then continue the form.")

    async def prefill(self, ex, snap: Snapshot, job: dict, page_no: int) -> PrefillResult:
        # Folded sections hide their fields from the prefill and the model (CenturyLink, 2026-10-21): open them first.
        if (snap.errors or {}).get("collapsed_sections"):
            from ..browser import snapshot as _snapmod
            if await fill.unfold_sections(ex):
                snap = await _snapmod.take(ex)
        return await super().prefill(ex, snap, job, page_no)


REGISTRY: dict[str, Adapter] = {a.portal: a for a in (Greenhouse(), Ashby(), Lever(), Workday(), Oracle(), ICIMS(),
                                                      SmartRecruiters(), SuccessFactors())}


def for_portal(ats: str) -> Adapter:
    return REGISTRY.get(ats or "", Adapter())
