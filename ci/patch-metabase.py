#!/usr/bin/env python3
"""Register this driver in a Metabase checkout. Run from the checkout's root.

Metabase has no hook for out-of-tree drivers: its deps files must name the driver before
`bin/build-driver.sh hardcharger` or its test runner can see it. The driver's own files are
expected at modules/drivers/hardcharger (a checkout, a copy or a symlink of this repository).

The edits are anchored on the Vertica entries, the last in each list, not on line numbers.
Already-patched files are left alone; a missing anchor is an error, never a silent skip, because
an unregistered driver fails much later with "could not locate namespace".
"""

import sys
from pathlib import Path

DRIVER = "hardcharger"

# (file, the text the anchor must follow, anchor, replacement)
EDITS = [
    # The driver's sources, as metabase/hardcharger.
    (
        "modules/drivers/deps.edn",
        ":deps",
        '  metabase/vertica            {:local/root "vertica"}}}',
        '  metabase/vertica            {:local/root "vertica"}\n'
        f'  metabase/{DRIVER}        {{:local/root "{DRIVER}"}}}}}}',
    ),
    # The driver's tests and test extensions, on the :drivers-dev test paths.
    (
        "deps.edn",
        "\n  :drivers-dev\n",
        '    "modules/drivers/vertica/test"]}',
        '    "modules/drivers/vertica/test"\n'
        f'    "modules/drivers/{DRIVER}/test"]}}',
    ),
]


def main() -> int:
    if not Path(f"modules/drivers/{DRIVER}/deps.edn").exists():
        print(f"modules/drivers/{DRIVER}/deps.edn: missing. Put this repository there first.", file=sys.stderr)
        return 1
    for name, after, old, new in EDITS:
        path = Path(name)
        if not path.exists():
            print(f"{name}: missing. Is this the root of a Metabase checkout?", file=sys.stderr)
            return 1
        text = path.read_text()
        if new in text:
            print(f"{name}: already registers {DRIVER}")
            continue
        start = text.find(after)
        at = text.find(old, start) if start >= 0 else -1
        if at < 0:
            print(
                f"{name}: anchor not found after {after!r}.\n"
                f"  anchor: {old!r}\n"
                "  Metabase has changed this file: update ci/patch-metabase.py.",
                file=sys.stderr,
            )
            return 1
        path.write_text(text[:at] + new + text[at + len(old) :])
        print(f"{name}: registered {DRIVER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
