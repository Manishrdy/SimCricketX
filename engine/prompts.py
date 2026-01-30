# -----------------------------------------------------------------------------
# prompts.py
# Templates for AI-driven cricket simulation
# -----------------------------------------------------------------------------

VALIDATION_PROMPT = """
You are a Cricket Match Director validation system. 
Analyze the following "Master Scenario" provided by a user for a simulation.

User Scenario:
"{scenario_text}"

Evaluate if this scenario is:
1. Clear enough to simulate.
2. Contains contradictions (e.g., "Team A wins but Team B chases target").
3. Feasible within a T20 match (e.g., scoring 1000 runs is not feasible).

Return a JSON object with this EXACT structure:
{{
    "score": <integer_0_to_10>,
    "is_valid": <boolean>,
    "feedback": "<string_explanation_for_user>"
}}
"""

MATCH_CHUNK_PROMPT = """
You are the AI Cricket Simulator. 
We are simulating a T20 match in 3-over chunks.
You must generate the outcomes for the NEXT 3 OVERS (or fewer if match ends).

**Current Match State**:
{context}

**Instructions**:
1. adhere strictly to the "Master Scenario" goals provided in the state.
2. You must select a VALID bowler for each over from the available bowlers list.
   - Do NOT select a bowler who has 0 overs remaining.
   - Do NOT select the same bowler twice in a row (consecutive overs) unless it satisfies "End One / Start Next" but generally allow rotation.
3. For each ball, provide:
   - "outcome": "0", "1", "2", "3", "4", "6", "Wicket", "Wide", "NoBall"
   - "commentary": A brief, exciting commentary line (max 15 words).
   - "wicket_type": If outcome is "Wicket", specify "Caught", "Bowled", "LBW", "Run Out", or "Stumped". Else null.
   - "is_extra": true if Wide/NoBall/Bye/LegBye.

**Format**:
Return ONLY a valid JSON object. Do not explain.
Structure:
{{
  "overs": [
    {{
      "over_number": <int>,
      "bowler_name": "<string_must_match_available_list>",
      "balls": [
        {{ "ball": 1, "outcome": "...", "commentary": "...", "wicket": <bool>, "wicket_type": "...", "is_extra": <bool> }},
        ... (up to 6 legal deliveries, plus extras if any)
      ]
    }}
  ]
}}
"""
