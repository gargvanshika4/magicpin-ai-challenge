from vera.replies import classify


def test_classifier():
    cases = {
        "Thank you for contacting us! Our team will respond shortly.": "auto_reply",
        "Aapki jaankari ke liye bahut-bahut shukriya. Main team tak pahuncha deti hoon.": "auto_reply",
        "Not interested. Stop messaging me.": "opt_out",
        "Stop messaging me. This is useless spam.": "opt_out",
        "This is useless, you are wasting my time": "hostile",
        "Btw can you also help me with my GST filing this month?": "off_topic",
        "Ok lets do it. Whats next?": "commit",
        "Yes please send the abstract. Also draft the patient WhatsApp.": "commit",
        "Mujhe magicpin judna hai": "commit",
        "haan kar do": "commit",
        "I'm in a meeting, later": "later",
        "No thanks": "decline",
        "How much does it cost?": "question",
        "Keratin mostly": "info",
    }
    for msg, want in cases.items():
        assert classify(msg) == want, (msg, classify(msg), want)


def test_conversation_handlers_respond(data):
    from conversation_handlers import respond
    t = data["triggers"]["trg_001_research_digest_dentists"]
    m = data["merchants"][t["merchant_id"]]
    st = {"conversation_id": "c1", "merchant_id": m["merchant_id"], "category": data["categories"]["dentists"],
          "merchant": m, "trigger": t}
    r = respond(st, "Yes please")
    assert r["action"] == "send" and "Done" in r["body"]
