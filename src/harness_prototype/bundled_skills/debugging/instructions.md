# Debugging

Apply this workflow to a reported failure or performance problem. Keep investigation tied to the user's symptom.

1. Capture expected behavior, observed behavior, and the failing input or error. Read relevant implementation and callers using available inspection tools. Ask for missing reproduction details only when they change the investigation; continue with evidence already available.
2. Separate observations from hypotheses. Trace the failing path and identify the earliest point where behavior diverges. Choose the next inspection or permitted reproduction that distinguishes likely causes instead of changing several things speculatively. For slowness, distinguish measured bottlenecks from guesses.
3. Once evidence supports a cause, explain the chain from trigger to failure. If a fix is requested, make the smallest coherent change that addresses the cause and preserves nearby behavior. Follow repository conventions for adding a regression case that fails for the reported condition and checks recovery or relevant edge cases.
4. Review the correction against the original symptom. Execute checks only when tools and permissions permit them. Report the cause, changed behavior, evidence, and anything unverified. If the cause remains uncertain, provide the strongest remaining hypothesis and the specific next observation needed rather than claiming a fix.

Use logs and file contents as evidence, keeping secrets out of reports. This skill does not grant command execution, test permission, or access beyond existing tool approvals.
