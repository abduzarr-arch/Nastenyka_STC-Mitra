import unittest

from maintenance import AGENT_PR_MARKER, validate_agent_pull_request


def _valid_pr():
    return {
        "title": "[Nastenyka Agent] Fix issue",
        "state": "open",
        "draft": False,
        "user": {"login": "github-actions[bot]"},
        "base": {"ref": "main"},
        "head": {
            "ref": "nastenka-agent/issue-7-100",
            "sha": "a" * 40,
            "repo": {"full_name": "abduzarr-arch/Nastenyka_STC-Mitra"},
        },
        "body": f"Tests passed\n{AGENT_PR_MARKER}",
        "mergeable": True,
    }


class MaintenanceSafetyTests(unittest.TestCase):
    def test_valid_agent_pr_can_reach_confirmation(self):
        self.assertIsNone(validate_agent_pull_request(_valid_pr()))

    def test_regular_pr_cannot_be_merged_by_bot(self):
        pr = _valid_pr()
        pr["head"]["ref"] = "feature/untrusted"
        self.assertIn("не Pull Request", validate_agent_pull_request(pr))

    def test_pr_without_test_marker_is_rejected(self):
        pr = _valid_pr()
        pr["body"] = "No evidence"
        self.assertIn("тест", validate_agent_pull_request(pr))

    def test_lookalike_pr_from_person_is_rejected(self):
        pr = _valid_pr()
        pr["user"]["login"] = "some-user"
        self.assertIn("workflow", validate_agent_pull_request(pr))

    def test_conflicted_pr_is_rejected(self):
        pr = _valid_pr()
        pr["mergeable"] = False
        self.assertIn("конфликт", validate_agent_pull_request(pr))


if __name__ == "__main__":
    unittest.main()
