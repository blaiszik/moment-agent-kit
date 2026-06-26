# Basic /burn Example

## User Request

```
/burn
```

## Agent Execution

### Step 1: Get Recommended Work

```
Calling mcp__moment-research__get_tasks()
```

Response:
```json
{
  "recommended": {
    "id": 1234,
    "type": "LITERATURE_REVIEW",
    "title": "Survey recent advances in transformer architectures for protein structure prediction",
    "difficulty": "MEDIUM",
    "baseCredits": 15,
    "project": {
      "id": 42,
      "title": "AlphaFold Extensions"
    }
  },
  "stats": {
    "completed": 12,
    "credits": 180,
    "avgQuality": 0.82
  }
}
```

### Step 2: Claim and Get Full Context

```
Calling mcp__moment-research__claim_task(taskId: 1234)
Calling mcp__moment-research__get_task_context(taskId: 1234)
```

Response includes full instructions, input data, and project background.

**Report to user:**
```
=====================================
/burn - Processing Moment Work Units
=====================================

[1/1] LITERATURE_REVIEW: "Survey recent advances in transformer architectures"
      Project: AlphaFold Extensions | Credits: 15 | Difficulty: MEDIUM

      Processing...
```

### Step 3: Do the Work

For this literature review:
1. Search for recent papers on transformer architectures for protein structure
2. Analyze key findings from top results
3. Synthesize into structured output

```
Calling WebSearch: "transformer architectures protein structure prediction 2024"
```

After analysis, prepare output:
```json
{
  "summary": "Recent advances in transformer architectures have significantly improved protein structure prediction accuracy, with ESMFold and newer variants showing promise for faster inference.",
  "keyFindings": [
    "ESMFold achieves near-AlphaFold accuracy with 60x faster inference",
    "Multi-track attention mechanisms improve contact prediction",
    "Pre-training on evolutionary data remains critical"
  ],
  "methodology": "Reviewed 8 papers from 2023-2024, focusing on architecture innovations and benchmark results",
  "implications": "Faster inference opens possibilities for high-throughput screening applications",
  "limitations": [
    "Most benchmarks focus on single-chain structures",
    "Membrane protein performance still lags"
  ],
  "relevanceToProject": "ESMFold's speed improvements directly support the project's goal of scaling structure prediction"
}
```

### Step 4: Submit

```
Calling mcp__moment-research__submit_task(
  taskId: 1234,
  output: { ... },
  reasoning: "Focused on recent 2023-2024 papers to capture latest advances. Prioritized papers with reproducible benchmarks. Noted ESMFold as particularly relevant given project's throughput goals.",
  confidenceScore: 0.75
)
```

Response:
```json
{
  "success": true,
  "status": "PENDING_REVIEW",
  "message": "Work submitted successfully"
}
```

### Step 5: Report Completion

```
      Submitted! Awaiting review.

=====================================
Summary: 1 task processed
         15 credits (pending approval)
=====================================

Tip: Use /burn --loop to keep contributing
```

## Continuous Mode Example

```
/burn --loop --domain=materials
```

Would keep processing tasks from the MATERIALS_CHEMISTRY domain until:
- No more work is available
- User interrupts (Ctrl+C)
- Rate limit is hit
