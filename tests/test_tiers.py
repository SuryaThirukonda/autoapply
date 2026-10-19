"""Confidence tiers: every answer and decision is confirmed, inferred or sensitive; finish events carry the
inferred list Surya can confirm or fix in one reply."""
import json
import sqlite3

from goat import answers, db, records, states


def _fresh(tmp_path):
    db.reset_for_tests(tmp_path / "t.db")
    with db.tx() as con:
        con.execute("INSERT INTO listings(id,company,title,url,ats) VALUES('l1','Acme','Intern','https://x.example/job','greenhouse')")
        con.execute("INSERT INTO jobs(id,listing_id,status,lane,priority) VALUES(1,'l1','filling','no_account',0)")


def test_answer_tier_by_source_and_key(tmp_path):
    _fresh(tmp_path)
    cases = [("questionnaire", "confirmed"), ("telegram", "confirmed"), ("seed:standing_answers.json", "confirmed"), ("hermes", "confirmed"),
             ("derived", "inferred"), ("default_no", "inferred"), ("only_option", "inferred"), ("profile", "inferred"), ("something_new", "inferred")]
    for src, tier in cases:
        assert answers.tier_for("preferences.team", src) == tier, src
    assert answers.tier_for("self_id.race_ethnicity", "questionnaire") == "sensitive"
    assert answers.tier_for("consent.sms", "telegram") == "sensitive"
    answers.save_answer("preferences.team", "Backend", answer_type="text", source="derived")
    assert answers.get("preferences.team").tier == "inferred"


def test_decision_tier_from_source_strings(tmp_path):
    _fresh(tmp_path)
    answers.save_answer("k.derived", "x", answer_type="text", source="derived")
    answers.save_answer("k.sure", "y", answer_type="text", source="questionnaire")
    assert answers.decision_tier("standing:k.derived (why)") == "inferred"
    assert answers.decision_tier("standing:k.sure") == "confirmed"
    assert answers.decision_tier("standing:missing.key") == "inferred"
    assert answers.decision_tier("standing:self_id.gender") == "sensitive"
    assert answers.decision_tier("policy:may_create_accounts") == "sensitive"
    assert answers.decision_tier("generated") == "inferred"
    assert answers.decision_tier("resume") == "confirmed"
    assert answers.decision_tier("cleared") == "confirmed"
    assert answers.decision_tier("user:manual") == "confirmed"


def test_record_decision_stores_the_tier_and_lists_inferred_ones(tmp_path):
    _fresh(tmp_path)
    answers.save_answer("preferences.team", "Backend", answer_type="text", source="derived")
    answers.save_answer("identity.first_name", "Surya", answer_type="text", source="questionnaire")
    records.record_decision(1, 1, "Team", "Backend", "standing:preferences.team", "e1")
    records.record_decision(1, 1, "First Name", "Surya", "standing:identity.first_name", "e2")
    records.record_decision(1, 1, "Why us", "Because", "generated", "e3")
    records.record_decision(1, 1, "Why us", "Because of X", "generated", "e3")     # re-set: the newest counts
    records.record_decision(1, 1, "Password", "secret", "policy:may_create_accounts", "e4")
    rows = records.decisions(1)
    assert [r["tier"] for r in rows] == ["inferred", "confirmed", "inferred", "inferred", "sensitive"]
    inf = records.inferred_decisions(1)
    assert [(i["n"], i["label"], i["value"], i["key"]) for i in inf] == [(1, "Team", "Backend", "preferences.team"), (2, "Why us", "Because of X", "")]
    text = records.inferred_text(inf)
    assert text.startswith("2 inferred: 1) Team = Backend; 2) Why us = Because of X") and text.endswith("Reply 'all good' or 'fix 2: <value>'.")
    assert len(records.inferred_text([{"n": i, "label": "L" * 40, "value": "V" * 40} for i in range(1, 13)])) <= 300
    assert records.inferred_text([]) == ""


def test_migration_adds_tier_to_an_old_database(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(str(p))
    con.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, page INTEGER, field_label TEXT, value TEXT, source TEXT, ref TEXT, ts INTEGER)")
    con.execute("INSERT INTO decisions(job_id,page,field_label,value,source,ref,ts) VALUES(1,1,'Email','a@x.com','resume','e1',1)")
    con.commit(); con.close()
    db.reset_for_tests(p)
    cols = {r[1] for r in db.connect().execute("PRAGMA table_info(decisions)")}
    assert "tier" in cols
    rows = records.decisions(1)
    assert rows[0]["tier"] is None                       # old rows keep no tier; new ones get one
    records.record_decision(1, 1, "Phone", "281", "resume", "e2")
    assert records.decisions(1)[-1]["tier"] == "confirmed"


def test_finish_events_carry_the_inferred_list(tmp_path):
    from goat import worker
    _fresh(tmp_path)
    answers.save_answer("preferences.team", "Backend", answer_type="text", source="derived")
    records.record_decision(1, 1, "Team", "Backend", "standing:preferences.team", "e1")
    worker.set_status(1, states.HOLD, reason="shadow_mode: gate passed", extra=worker.finish_extra(1))
    ev = json.loads(db.connect().execute("SELECT detail FROM events WHERE kind='job:hold' ORDER BY n DESC LIMIT 1").fetchone()[0])
    assert ev["reason"].startswith("shadow_mode") and ev["inferred"][0]["label"] == "Team" and ev["inferred"][0]["n"] == 1
    worker.set_status(1, states.HOLD, reason="eligibility: x")
    ev = json.loads(db.connect().execute("SELECT detail FROM events WHERE kind='job:hold' ORDER BY n DESC LIMIT 1").fetchone()[0])
    assert "inferred" not in ev
