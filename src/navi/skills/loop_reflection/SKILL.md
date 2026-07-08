---
name: Loop Reflection
description: A workflow to automatically recover when you receive a loop warning or get stuck repeatedly trying the same failing approach.
permission: read
source: local
tags: [loop, debugging, stuck, reflection]
---

# Loop Reflection Skill

Use this skill immediately when the system returns a loop progress fact (e.g., `fact_type: loop_progress_fact` with `reason: REPEATED_PROGRESS_SIGNATURE`). It prevents you from repeatedly failing and wasting tokens.

When you are stuck in a loop, your current context might be polluted with failing assumptions. This skill is a fact-collection procedure for recovering situational awareness; it is not a tool-routing rule and it does not make an independent plan executable by itself.

## Workflow

1. **Stop Repeating**: Record the repeated operation, arguments, and observed result before attempting another action.
2. **Collect Loop Facts**: Build a concise fact packet with:
   - The original objective.
   - The exact sequence of tools and arguments already tried.
   - The specific errors or facts received.
   - The loop warning or repeated progress signature.
   - Current capability, permission, approval, and workspace constraints.
3. **Request Independent Analysis When Available**: If the current capability manifest exposes a governed delegation or review capability, it may be used to obtain independent analysis facts. The request should ask for failure-domain analysis, contradicted assumptions, missing facts, and candidate alternatives, not an executable script.
4. **Re-plan From Facts**: Treat any independent analysis as fact data. The model still chooses the next declared syscall from the current manifest, constraints, approval state, and collected facts.
5. **Surface a Bounded Failure When Needed**: If the collected facts show that no valid capability path remains, report the blocking facts and missing capability or approval state.
