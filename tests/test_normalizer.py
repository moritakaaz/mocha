"""Unit tests: Slack mrkdwn to Discord markdown conversion."""

import unittest

from bot.normalizer import build_message, convert_text, sender_name_for


class TestConvertText(unittest.TestCase):
    def test_labeled_link(self):
        # Plain text form, never a masked markdown link.
        self.assertEqual(
            convert_text("see <https://x.test/a|this doc> now"),
            "see this doc (https://x.test/a) now")

    def test_link_label_equal_to_url_collapses(self):
        # Slack stores a pasted link as <url|url>; the redundant
        # label must not survive in any form.
        self.assertEqual(
            convert_text("go <https://x.test/a|https://x.test/a> now"),
            "go https://x.test/a now")

    def test_link_label_without_scheme_collapses(self):
        # Slack's stored label drops the scheme; it is still the URL
        # itself, so the address must not be printed twice.
        self.assertEqual(
            convert_text(
                "see <https://github.com/thomasloupe/Slackord"
                "|github.com/thomasloupe/Slackord> now"),
            "see https://github.com/thomasloupe/Slackord now")

    def test_link_label_modulo_trailing_slash_collapses(self):
        self.assertEqual(
            convert_text("go <https://x.test/a/|https://x.test/a> now"),
            "go https://x.test/a/ now")
        self.assertEqual(
            convert_text("go <https://x.test/a|x.test/a/> now"),
            "go https://x.test/a now")

    def test_link_label_case_insensitive_collapses(self):
        self.assertEqual(
            convert_text("go <https://x.test/A|X.TEST/a> now"),
            "go https://x.test/A now")

    def test_link_different_label_does_not_collapse(self):
        # A label that points somewhere else is a real label and is
        # kept in the plain-text "label (url)" form.
        self.assertEqual(
            convert_text("go <https://x.test/a|other.test/b> now"),
            "go other.test/b (https://x.test/a) now")
        self.assertEqual(
            convert_text("go <https://x.test/a|x.test/b> now"),
            "go x.test/b (https://x.test/a) now")

    def test_bare_link(self):
        self.assertEqual(convert_text("visit <https://x.test>"),
                         "visit https://x.test")

    def test_mailto_label_equal_to_address_collapses(self):
        # Slack stores a typed email as <mailto:a@b.co|a@b.co>; the
        # Discord text must be the bare address, not the raw markup.
        self.assertEqual(
            convert_text("mail <mailto:ana@x.co|ana@x.co> ya"),
            "mail ana@x.co ya")

    def test_mailto_bare(self):
        self.assertEqual(convert_text("mail <mailto:ana@x.co> ya"),
                         "mail ana@x.co ya")

    def test_mailto_real_label_kept(self):
        self.assertEqual(
            convert_text("mail <mailto:ana@x.co|Ana's email> ya"),
            "mail Ana's email (ana@x.co) ya")

    def test_user_mention_with_resolver(self):
        got = convert_text(
            "hi <@U123>", resolve_user=lambda uid: "Ana" if uid == "U123"
            else None)
        self.assertEqual(got, "hi @Ana")

    def test_user_mention_with_label_fallback(self):
        self.assertEqual(convert_text("hi <@U999|bob>"), "hi @bob")

    def test_channel_mention(self):
        self.assertEqual(convert_text("join <#C999|general>"),
                         "join #general")

    def test_channel_resolver_discord_mention_passes_through(self):
        # A resolver that returns a Discord mention (<#id>) gets it
        # emitted verbatim, so Discord renders a clickable link.
        got = convert_text(
            "ke <#C7> yuk",
            resolve_channel=lambda cid: "<#D7>" if cid == "C7" else None)
        self.assertEqual(got, "ke <#D7> yuk")

    def test_special_mentions(self):
        self.assertEqual(convert_text("<!channel> <!here> <!everyone>"),
                         "@channel @here @everyone")

    def test_bold_and_strike(self):
        self.assertEqual(convert_text("*bold* and ~gone~"),
                         "**bold** and ~~gone~~")

    def test_code_protection(self):
        self.assertEqual(convert_text("`<@U1>` and ```*not bold*```"),
                         "`<@U1>` and ```*not bold*```")

    def test_html_entities(self):
        self.assertEqual(convert_text("a &amp; b"), "a & b")

    def test_empty(self):
        self.assertEqual(convert_text(""), "")


class TestSenderName(unittest.TestCase):
    USER = {"name": "ana_user", "display_name": "Ana",
            "real_name": "Ana Kreavia"}

    def test_display(self):
        self.assertEqual(sender_name_for(self.USER, "display"), "Ana")

    def test_real(self):
        self.assertEqual(sender_name_for(self.USER, "real"),
                         "Ana Kreavia")

    def test_user(self):
        self.assertEqual(sender_name_for(self.USER, "user"), "ana_user")

    def test_unknown_name_format_defaults_to_display(self):
        self.assertEqual(sender_name_for(self.USER, "weird"), "Ana")

    def test_missing_user(self):
        self.assertEqual(sender_name_for(None, "display", "fallback"),
                         "fallback")


class TestBuildMessage(unittest.TestCase):
    def test_basic(self):
        msg = build_message(
            {"ts": "1700000000.000100", "user": "U1",
             "text": "Hello <@U1> *team*"},
            "C1", get_user=lambda uid: {"display_name": "Ana",
                                        "avatar_url": "https://i/a.png"})
        self.assertIsNotNone(msg)
        self.assertEqual(msg.text, "Hello @Ana **team**")
        self.assertEqual(msg.sender_name, "Ana")
        self.assertEqual(msg.sender_avatar, "https://i/a.png")
        self.assertFalse(msg.is_thread_reply)

    def test_thread_reply_flag(self):
        msg = build_message({"ts": "2.2", "thread_ts": "1.1",
                             "text": "reply"}, "C1")
        self.assertTrue(msg.is_thread_reply)

    def test_files_reduced(self):
        msg = build_message(
            {"ts": "1.1",
             "files": [{"name": "a.png", "size": 10,
                        "mimetype": "image/png",
                        "url_private": "https://f/a"}]}, "C1")
        self.assertEqual(len(msg.files), 1)
        self.assertEqual(msg.files[0].url, "https://f/a")

    def test_system_subtype_dropped(self):
        self.assertIsNone(build_message(
            {"ts": "1.1", "subtype": "channel_join"}, "C1"))

    def test_huddle_thread_dropped(self):
        self.assertIsNone(build_message(
            {"ts": "1.2", "subtype": "huddle_thread", "user": "USLACKBOT",
             "text": ""}, "C1"))

    def test_missing_ts_dropped(self):
        self.assertIsNone(build_message({"text": "no ts"}, "C1"))


if __name__ == "__main__":
    unittest.main()
