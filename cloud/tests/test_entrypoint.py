"""The wrapper's argument handling, exercised by running the real script.

Only the part that runs before any network or runtime call is tested here: the
digest argument and its validation. Each case stops at a known, explicit exit,
so nothing is fetched and no `sg` is needed.

Skipped on Windows, where `bash` may resolve to the WSL launcher rather than a
shell; the Linux CI runner executes it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import unittest

import _bootstrap

ENTRYPOINT = _bootstrap.REPO / "cloud" / "entrypoint.sh"
DIGEST = "sha256:" + "0123456789abcdef" * 4


@unittest.skipIf(os.name == "nt", "bash may be the WSL launcher on Windows")
@unittest.skipIf(shutil.which("bash") is None, "bash is not available")
class EntrypointArgumentTests(unittest.TestCase):
    def run_entrypoint(self, *arguments: str) -> subprocess.CompletedProcess:
        environment = {
            key: value for key, value in os.environ.items() if key != "CONFIG_URI"
        }
        return subprocess.run(
            ["bash", str(ENTRYPOINT), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def test_a_well_formed_digest_is_accepted(self) -> None:
        # Parsing succeeds, so the script reaches the next check and stops
        # there, at the missing configuration.
        result = self.run_entrypoint(f"--container-digest={DIGEST}")
        self.assertEqual(result.returncode, 2)
        self.assertIn("CONFIG_URI is not set", result.stderr)

    def test_a_malformed_digest_is_refused(self) -> None:
        result = self.run_entrypoint("--container-digest=latest")
        self.assertEqual(result.returncode, 2)
        self.assertIn("malformed container digest", result.stderr)

    def test_an_unknown_argument_is_refused(self) -> None:
        result = self.run_entrypoint("--publish-anyway")
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown argument", result.stderr)

    def test_no_argument_is_still_a_valid_invocation(self) -> None:
        result = self.run_entrypoint()
        self.assertEqual(result.returncode, 2)
        self.assertIn("CONFIG_URI is not set", result.stderr)


if __name__ == "__main__":
    unittest.main()
