"""Cisco (Workday, 2026-10-23): "Which post-secondary education opportunity are you currently pursuing?" listed
'Other Diploma/Degree/Certification'; the consent pass read 'Certification' as an 'I certify' box, ticked it as his
truthfulness attestation, and the judge failed the draft. A certification held is a credential, not a consent."""
from goat.adapters import consent_key
from goat.agent.tools import consent_question_key

GROUP = "Which post-secondary education opportunity are you currently pursuing?"


def test_a_credential_option_is_no_consent():
    assert consent_key(f"Other Diploma/Degree/Certification {GROUP}") is None
    assert consent_key("Professional Certification") is None
    assert consent_key("Do you hold any industry certifications or licenses?") is None
    assert consent_question_key("Do you hold any professional certifications?") is None


def test_an_attestation_still_maps_to_his_standing_yes():
    assert consent_key("I certify that the information in this application is true and complete") == "consent.truthfulness_attestation"
    assert consent_key("Certification of the accuracy of my application") == "consent.truthfulness_attestation"
    assert consent_key("By checking this box I certify my education history and degree are accurate") == "consent.truthfulness_attestation"
    assert consent_key("I agree to the privacy notice") == "consent.privacy_notice"
