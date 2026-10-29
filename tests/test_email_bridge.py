import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import base64
import subprocess

import dkim

from app import email_bridge as eb


def keypair():
    private = subprocess.run(["openssl", "genrsa", "2048"], capture_output=True, check=True).stdout
    public = subprocess.run(["openssl", "rsa", "-pubout", "-outform", "DER"], input=private,
                            capture_output=True, check=True).stdout
    return private, b"v=DKIM1; k=rsa; p=" + base64.b64encode(public)


PRIVATE, PUBLIC = keypair()


def dns(name, timeout=5):
    return PUBLIC if name.startswith(b"s1._domainkey.") else b""


def verify(raw, index):
    return bool(dkim.DKIM(raw).verify(idx=index, dnsfunc=dns))


def mail(sender="me@gmail.com", body="did the nightly run finish?", sign_domain="gmail.com", attach=None):
    msg = EmailMessage()
    msg["From"] = f"Me <{sender}>"
    msg["To"] = "mybot@zhoulab.io"
    msg["Subject"] = "nightly run"
    msg["Message-ID"] = "<abc@mail.gmail.com>"
    msg.set_content(body + "\n\nOn Mon, someone wrote:\n> old stuff\n")
    if attach:
        name, data = attach
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    raw = msg.as_bytes(policy=policy.SMTP)
    if sign_domain:
        raw = dkim.sign(raw, b"s1", sign_domain.encode(), PRIVATE, include_headers=[b"from", b"subject", b"to"]) + raw
    return raw


class FakeBridge(eb.EmailBridge):
    def __init__(self, tmp):
        cfg = eb.MailConfig()
        cfg.url, cfg.token, cfg.address, cfg.owners = "https://w", "t", "mybot@zhoulab.io", {"me@gmail.com"}
        super().__init__(config=cfg, server_url="http://127.0.0.1:1", owner_actor_id="42", state_dir=tmp)
        self.sent, self.asked = [], []

    def send(self, to, msg):
        self.sent.append((to, msg))

    def _ask(self, question, thread):
        self.asked.append((question, thread))
        return "It finished at **04:10**."


class EmailBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.downloads = Path(self.tmp.name) / "Downloads"
        self.downloads.mkdir()
        self._old = eb.DOWNLOADS, eb.dkim_ok
        eb.DOWNLOADS = self.downloads
        real = self._old[1]
        eb.dkim_ok = lambda raw, addr, verify_fn=None: real(raw, addr, verify_fn or verify)
        self.bridge = FakeBridge(self.tmp.name)

    def tearDown(self):
        eb.DOWNLOADS, eb.dkim_ok = self._old
        self.tmp.cleanup()

    def test_dkim_alignment(self):
        self.assertTrue(self._old[1](mail(), "me@gmail.com", verify))
        # Signed, but by the attacker's own domain: not aligned with From.
        self.assertFalse(self._old[1](mail(sign_domain="evil.example"), "me@gmail.com", verify))
        self.assertFalse(self._old[1](mail(sign_domain=None), "me@gmail.com", verify))
        tampered = mail().replace(b"Subject: nightly run", b"Subject: transfer money")
        self.assertFalse(self._old[1](tampered, "me@gmail.com", verify))

    def test_owner_mail_is_answered_in_thread(self):
        self.assertEqual(self.bridge.handle(mail()), "answered")
        question, thread = self.bridge.asked[0]
        self.assertIn("did the nightly run finish?", question)
        self.assertNotIn("old stuff", question)
        self.assertEqual(thread, "<abc@mail.gmail.com>")
        to, reply = self.bridge.sent[0]
        self.assertEqual(to, "me@gmail.com")
        self.assertEqual(reply["Subject"], "Re: nightly run")
        self.assertEqual(reply["In-Reply-To"], "<abc@mail.gmail.com>")
        self.assertEqual(reply["Auto-Submitted"], "auto-replied")
        html = reply.get_body(preferencelist=("html",)).get_content()
        self.assertIn("<strong>04:10</strong>", html)
        # Seen once: a redelivery isn't answered twice.
        self.assertEqual(self.bridge.handle(mail()), "duplicate")

    def test_forged_or_foreign_mail_is_ignored(self):
        self.assertEqual(self.bridge.handle(mail(sign_domain="evil.example")), "unverified")
        self.assertEqual(self.bridge.handle(mail(sender="other@gmail.com")), "not owner")
        self.assertEqual(self.bridge.sent, [])

    def test_attachments_land_in_downloads_once(self):
        (self.downloads / "paper.pdf").write_bytes(b"%PDF same")
        self.bridge.handle(mail(attach=("paper.pdf", b"%PDF same")))
        self.assertIn("already in Downloads", self.bridge.asked[0][0])
        msg = BytesParser(policy=policy.default).parsebytes(mail(attach=("paper.pdf", b"%PDF other")))
        saved = eb.save_attachments(msg, self.downloads)
        self.assertEqual(Path(saved[0]["path"]).name, "paper (1).pdf")
        self.assertEqual(sorted(p.name for p in self.downloads.iterdir()), ["paper (1).pdf", "paper.pdf"])

    def test_only_the_owner_is_ever_emailed(self):
        bridge = eb.EmailBridge(config=self.bridge.config, server_url="x", owner_actor_id="42", state_dir=self.tmp.name)
        with self.assertRaises(ValueError):
            bridge.send("someone@else.org", EmailMessage())

    def test_figures_go_inline(self):
        png = Path(self.tmp.name) / "a1-flow.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        msg = eb.new_message(bot_address="mybot@zhoulab.io", to="me@gmail.com", subject="Pulse",
                             text="Intro\n\n![How it flows](figure:a1-flow.png)\n\nEnd", images={"a1-flow.png": png})
        raw = msg.as_string()
        self.assertIn("cid:", raw)
        self.assertIn("Figure: How it flows", msg.get_body(preferencelist=("plain",)).get_content())


if __name__ == "__main__":
    unittest.main()
