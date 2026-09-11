"""QA: auto-generated chat names when the user provides no title.

Tests:

- NamingFormatTests: unit tests against generate_name() for the
  {transport} : {n} : {task} format, counter behaviour, and filler stripping.

- NamingFallbackTests: edge cases for _extract_task_words (all-fillers,
  short words, capitalisation).

The HTTP-level / DB-level flow is covered by the composer browser test
(test_qa_composer_chat_name.py). _write_agent_name sits inside the turn
completions handler which the browser tests already exercise end-to-end.
"""
from __future__ import annotations

import unittest


class NamingFallbackTests(unittest.TestCase):
    """_extract_task_words edge cases — the function that feeds generate_name."""

    def test_all_fillers_returns_empty(self):
        from routes.naming import _extract_task_words

        words = _extract_task_words("the a an is on at")
        self.assertEqual(len(words), 0, "all-fillers must yield empty")

    def test_single_meaningful_word(self):
        from routes.naming import _extract_task_words

        words = _extract_task_words("fix the bug")
        # Returns capitalized words
        lower_words = [w.lower() for w in words]
        self.assertIn("fix", lower_words)
        self.assertIn("bug", lower_words)

    def test_under_three_char_verbs_kept(self):
        from routes.naming import _extract_task_words

        words = _extract_task_words("fix add run hit")
        # fix/add/run/hit are all in _SHORT_KEEP
        lower_words = [w.lower() for w in words]
        self.assertIn("fix", lower_words)
        self.assertIn("add", lower_words)
        self.assertIn("run", lower_words)
        self.assertIn("hit", lower_words)

    def test_under_three_char_nouns_kept(self):
        from routes.naming import _extract_task_words

        words = _extract_task_words("the cat sat on the mat")
        # "the", "on" are fillers; "cat", "sat", "mat" >= 3 chars stay
        lower_words = [w.lower() for w in words]
        self.assertIn("cat", lower_words)
        self.assertIn("mat", lower_words)

    def test_mixed_fillers_and_words(self):
        from routes.naming import _extract_task_words

        words = _extract_task_words("i need to add the login validation")
        lower_words = [w.lower() for w in words]
        self.assertIn("add", lower_words)
        self.assertIn("login", lower_words)
        self.assertIn("validation", lower_words)
        self.assertNotIn("i", lower_words)
        self.assertNotIn("to", lower_words)


class NamingFormatTests(unittest.TestCase):
    """generate_name produces the expected {transport} : {n} : {task} format."""

    def setUp(self):
        import routes.naming
        routes.naming._spawn_counter.clear()

    def test_basic_format(self):
        from routes.naming import generate_name

        name = generate_name("localhost", "fix the auth bug in login")
        self.assertIn("localhost", name)
        self.assertIn("fix", name.lower())
        self.assertIn("auth", name.lower())
        self.assertIn("login", name.lower())

    def test_filler_words_are_stripped(self):
        from routes.naming import generate_name

        name = generate_name("ssh", "the fix to the problem of the crash")
        self.assertIn("ssh", name)
        task_part = name.split(" : ", 2)[2] if " : " in name else ""
        self.assertNotIn(" the ", task_part)
        self.assertNotIn(" of ", task_part)

    def test_empty_prompt_falls_back(self):
        from routes.naming import generate_name

        name = generate_name("local", "")
        self.assertIn("local : 1 : Untitled task", name)

    def test_short_words_kept_if_verbs(self):
        from routes.naming import generate_name

        name = generate_name("local", "fix add run")
        self.assertIn("fix", name.lower())
        self.assertIn("add", name.lower())
        self.assertIn("run", name.lower())

    def test_counter_increases_per_transport(self):
        from routes.naming import generate_name

        name1 = generate_name("local", "task one")
        name2 = generate_name("local", "task two")
        self.assertIn(" : 1 :", name1)
        self.assertIn(" : 2 :", name2)

    def test_counter_is_independent_per_transport(self):
        from routes.naming import generate_name

        n1a = generate_name("local", "task")
        n2a = generate_name("remote", "task")
        n1b = generate_name("local", "task")
        n2b = generate_name("remote", "task")
        self.assertIn(" : 2 :", n1b)
        self.assertIn(" : 2 :", n2b)

    def test_task_words_limited_to_five(self):
        from routes.naming import generate_name

        name = generate_name("local", "fix the auth bug in the login module")
        task_part = name.split(" : ", 2)[2] if " : " in name else ""
        self.assertLessEqual(len(task_part.split()), 5,
                             "task portion should be at most 5 words")


if __name__ == "__main__":
    unittest.main()
