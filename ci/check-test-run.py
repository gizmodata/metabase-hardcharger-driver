#!/usr/bin/env python3
"""Check that a run of Metabase's test runner was a real, green run of this driver's suite.

    ci/check-test-run.py LOG [--partitions N] [--only]

The runner's own exit code says whether a test failed. This says whether the run proves anything:
it ran against the hardcharger driver and nothing else, it finished, it ran at least as many tests
as a full run should (a run that selects nothing is green and proves nothing), and its summary
counts no failure and no error.

--partitions N: the log is one of N partitions of the suite, so expect a share of the tests.
--only: the run named its tests (one namespace): expect at least one test.
"""

import argparse
import re
import sys

# A full run on Metabase 0.65.4.3 is 3,5xx tests and 24,0xx assertions. The floors leave room for
# Metabase adding and removing tests between patch releases, not for a suite that mostly didn't run.
FULL_RUN_MIN_TESTS = 3000
FULL_RUN_MIN_ASSERTIONS = 20000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log")
    parser.add_argument("--partitions", type=int, default=1)
    parser.add_argument("--only", action="store_true")
    args = parser.parse_args()

    text = re.sub(r"\x1b\[[0-9;]*m", "", open(args.log, errors="replace").read())
    problems = []

    drivers = re.findall(r"Running QP tests against these drivers: #\{([^}]*)\}", text)
    if not drivers:
        problems.append("the runner never said which drivers it tests")
    elif any(d.strip() != ":hardcharger" for d in drivers):
        problems.append(f"the run was not against the hardcharger driver alone: {sorted(set(drivers))}")

    ran = re.findall(r"^Ran (\d+) tests in [\d.]+ seconds$", text, flags=re.M)
    counts = re.findall(r"^(\d+) assertions, (\d+) failures, (\d+) errors\.$", text, flags=re.M)
    if len(ran) != 1 or len(counts) != 1:
        problems.append(f"expected one summary, found {len(ran)} 'Ran N tests' and {len(counts)} 'N assertions' lines: "
                        "the run did not finish")
    else:
        tests, (assertions, failures, errors) = int(ran[0]), map(int, counts[0])
        if args.only:
            min_tests, min_assertions = 1, 1
        else:
            # a partition holds about 1/N of the tests; allow it to be a third lighter than that
            min_tests = FULL_RUN_MIN_TESTS * 2 // (3 * args.partitions) if args.partitions > 1 else FULL_RUN_MIN_TESTS
            min_assertions = (FULL_RUN_MIN_ASSERTIONS // (3 * args.partitions) if args.partitions > 1
                              else FULL_RUN_MIN_ASSERTIONS)
        print(f"{tests} tests, {assertions} assertions, {failures} failures, {errors} errors "
              f"(at least {min_tests} tests and {min_assertions} assertions expected)")
        if tests < min_tests:
            problems.append(f"only {tests} tests ran; at least {min_tests} expected")
        if assertions < min_assertions:
            problems.append(f"only {assertions} assertions ran; at least {min_assertions} expected")
        if failures or errors:
            problems.append(f"{failures} failures and {errors} errors")
    if "All tests passed." not in text:
        problems.append("the runner did not print 'All tests passed.'")

    failed = sorted(set(re.findall(r"^(?:FAIL|ERROR) in (\S+)", text, flags=re.M)))
    if failed:
        print(f"{len(failed)} tests failed:")
        for name in failed:
            print(f"  {name}")
    for problem in problems:
        print(f"NOT A GREEN RUN: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
