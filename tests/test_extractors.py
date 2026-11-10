from src.extractors import extract_emails, extract_social_links, is_valid_email


def test_plain_email_extracted():
    assert extract_emails("Contact me: hello@creator.in for anything") == ["hello@creator.in"]


def test_business_email_preferred_over_personal():
    text = "Personal: ravi@gmail.com\nFor business enquiries: deals@ravitech.in"
    assert extract_emails(text)[0] == "deals@ravitech.in"


def test_obfuscated_email_is_decoded_not_guessed():
    assert extract_emails("mail: tech.guru [at] gmail [dot] com") == ["tech.guru@gmail.com"]
    assert extract_emails("collab (at) studio (dot) co (dot) in") == ["collab@studio.co.in"]


def test_junk_addresses_rejected():
    text = "logo@2x.png noreply@youtube.com someone@example.com a@b"
    assert extract_emails(text) == []
    assert not is_valid_email("Not Found")


def test_no_email_returns_empty():
    assert extract_emails("Subscribe for more videos! Business inquiries via DM") == []


def test_social_links_and_website():
    text = (
        "Follow me\nhttps://www.instagram.com/tech_ravi/?hl=en\nhttps://instagram.com/p/abc123\n"
        "TikTok https://www.tiktok.com/@ravitech  X: https://twitter.com/ravitech\n"
        "Gear: https://amzn.to/xyz  Site: https://ravitech.in/about."
    )
    links = extract_social_links(text)
    assert links["instagram"] == "https://www.instagram.com/tech_ravi/"
    assert links["tiktok"] == "https://www.tiktok.com/@ravitech"
    assert links["twitter"] == "https://x.com/ravitech"
    assert links["website"] == "https://ravitech.in/about"


def test_bare_instagram_link():
    assert extract_social_links("insta: instagram.com/coder.priya")["instagram"] == "https://www.instagram.com/coder.priya/"
