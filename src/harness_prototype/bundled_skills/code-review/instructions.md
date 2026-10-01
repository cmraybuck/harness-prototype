# Code review

Apply this workflow when the user requests a review. When another task is requested, follow that task rather than turning it into a review.

1. Establish the review scope from the supplied code, files, or change reference. If repository inspection is available, read project guidance and inspect the relevant diff, surrounding code, callers, and tests. Otherwise review the supplied material and state the limits of that evidence.
2. Trace changed behavior through inputs, state transitions, and error handling. Prioritize correctness, data loss, security boundaries, compatibility, and regressions. Check whether existing tests exercise the risky behavior; distinguish absent coverage from a proven bug.
3. Report only actionable findings supported by the inspected code. For each finding give severity, file and line when known, the triggering condition, practical impact, and a focused correction. Label uncertain conclusions and identify what evidence would resolve them. Keep style suggestions separate and include them only when requested or required by project conventions.
4. End with the reviewed scope and verification limits. If no concrete findings emerge, say so without claiming the code is bug-free. A review request authorizes inspection, not edits; make fixes when the user requests them.

Use available tools under their existing permissions. State whether tests were actually executed; suggested commands and inspected tests are not execution evidence.
