import unittest
from pathlib import Path
from domain_context.loader import load_domain

class DomainFixtureTest(unittest.TestCase):
    def test_fixture_is_complete(self):
        value = load_domain(Path("fixtures/domain.json"))
        self.assertEqual(value["domain"], "multi-channel-screening")
        self.assertGreaterEqual(len(value["facts"]), 2)

if __name__ == "__main__":
    unittest.main()
