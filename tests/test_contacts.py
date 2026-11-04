from moh_leadgen.enrich.extract import (
    apply_email_pattern,
    infer_email_pattern,
    is_generic_mailbox,
    personal_mailboxes,
)
from moh_leadgen.models import Person


def test_generic_vs_personal():
    assert is_generic_mailbox("support@x.com") and is_generic_mailbox("patientsupport@x.com")
    assert is_generic_mailbox("nevada-privacy@x.com") and is_generic_mailbox("customer.service@x.com")
    assert is_generic_mailbox(".+ready-4month@x.com")
    assert not is_generic_mailbox("randi@x.com") and not is_generic_mailbox("jane.doe@x.com")
    assert personal_mailboxes(["info@x.com", "jdoe@x.com"]) == ["jdoe@x.com"]


def test_pattern_inference_and_application():
    people = [Person(name="Jane Doe"), Person(name="Dr. Sam Lee, MD")]
    assert infer_email_pattern(["jane.doe@x.com"], people) == "{first}.{last}"
    assert infer_email_pattern(["jdoe@x.com"], people) == "{f}{last}"
    assert infer_email_pattern(["randi@x.com"], people) == "{first}"
    assert infer_email_pattern(["info@x.com"], people) is None
    assert apply_email_pattern("{first}.{last}", "Dr. Sam Lee, MD", "x.com") == "sam.lee@x.com"
    assert apply_email_pattern("{f}{last}", "Anne Fulenwider", "myalloy.com") == "afulenwider@myalloy.com"
    assert apply_email_pattern("{first}", "Madonna", "x.com") is None
