"""Texas Instruments and Westinghouse (2026-10-25): the run's job carries no résumé until the prefill uploads one, the brief
said 'resume file to attach: None', and both runs held 'the job brief provides no résumé file to upload'."""
import goat.gate as gate_mod
from goat.agent.prompts import job_brief


def test_the_brief_names_the_jobs_resume_before_it_is_uploaded(monkeypatch):
    monkeypatch.setattr(gate_mod, "job_resume_name", lambda job: "Surya_Thirukonda_Resume_SWE.pdf")
    text = job_brief({"company": "Texas Instruments", "resume_file": None}, [])
    assert "resume file to attach: Surya_Thirukonda_Resume_SWE.pdf (not attached yet" in text and "resume file to attach: None" not in text
    assert "resume file to attach: Surya_Thirukonda_Resume_SWE.pdf\n" in job_brief({"company": "TI", "resume_file": "Surya_Thirukonda_Resume_SWE.pdf"}, []) + "\n"


def test_a_job_with_no_resume_says_so(monkeypatch):
    monkeypatch.setattr(gate_mod, "job_resume_name", lambda job: "")
    assert "resume file to attach: none configured for this job" in job_brief({"company": "Acme", "resume_file": None}, [])
