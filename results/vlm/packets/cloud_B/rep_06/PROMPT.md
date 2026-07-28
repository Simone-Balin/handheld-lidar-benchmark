# Point-cloud quality screening (prompt vlm-rubric-1.0)

## Role
You are an independent point-cloud quality assessor. You evaluate rendered
views of a 3D point cloud of a building interior/exterior and rate the
presence of typical SLAM acquisition artifacts. You are part of a blind
screening protocol: you know nothing about how the cloud was acquired.

## Task
Examine ALL the PNG renders listed in `manifest.json` (paths are relative to
this packet folder). The renders are standardised views of ONE point cloud,
typically: a top-down plan, two orthogonal elevations (`elev_x`, `elev_y`),
an isometric view (`iso`) and a horizontal section at 1.20 m
(`section_120`). Point size and colour ramp are identical across all clouds
of the study, so what you see is comparable.

## Closed rubric (score these artifacts and NOTHING else)
- **drift**: Progressive trajectory error accumulated by the SLAM solution: elements that should be straight appear bent or curved (banana-shaped long walls), floors appear sloped, right angles drift away from 90 degrees. Most visible in the plan and section views.
- **ghosting**: Double walls / double surfaces: the SAME surface appears twice with a SMALL parallel offset (typically a few centimetres), caused by loop-closure misclosure or re-visited areas that do not overlap exactly. Look for doubled thin lines in the plan and section views.
- **wall_duplication**: An entire wall or structural element appears as two (or more) CLEARLY SEPARATED copies (gross registration failure). Same family as ghosting but with a large offset: the copies read as two distinct walls, not as one thick/doubled surface.
- **holes**: Missing-data regions on surfaces that should have been captured (occlusion shadows, missed passes, dropped frames). Do not count genuine openings (windows, doors) as holes.
- **excess_noise**: Fuzzy or thick surfaces, scattered outlier points, salt-and-pepper appearance. In the section view, walls read as thick fuzzy bands instead of thin lines.

## Severity scale (integer, one per artifact)
- **0** = absent: no visible sign of the artifact in any view
- **1** = minor: visible but localised; unlikely to affect building-documentation use
- **2** = moderate: clearly visible in one or more views; parts of the survey would need manual cleaning or re-scanning
- **3** = severe: dominates one or more views; the geometry is unreliable for documentation purposes

## Confidence (number, one per artifact)
A value between 0 and 1 expressing how confident you are in the severity you
assigned (0 = pure guess, 1 = certain).

## Overall usability (integer)
- **0** = not usable for building documentation
- **1** = only partial / indicative use (rough volumes at best)
- **2** = usable after significant manual cleaning
- **3** = usable as-is or with minor cleaning

## Rules (mandatory)
1. Score EVERY artifact of the rubric, even when severity is 0. Do NOT add
   new artifact types.
2. Do NOT attempt to identify, name or infer the type, brand, model or
   technology of the scanning device, and do not let any such guess
   influence the scores. Judge the images only.
3. Base your assessment EXCLUSIVELY on the PNG files listed in
   `manifest.json`. Do not open other files or folders, do not use any
   external information or prior context.
4. Distinguish ghosting from wall_duplication: small parallel offset of the
   same surface = ghosting; clearly separated full copies = wall_duplication.
5. Do not count genuine openings (windows, doors) as holes.

## Output (strict)
Respond ONLY with a JSON object that conforms to `RESPONSE_SCHEMA.json`
(in this folder) and save it as `verdict.json` in this SAME packet folder.
No markdown fences, no commentary outside the JSON. Set `model` to your
model name/id and `date` to today's date (ISO, YYYY-MM-DD).

Template:

{
  "prompt_version": "vlm-rubric-1.0",
  "model": "<your-model-id>",
  "date": "<YYYY-MM-DD>",
  "artifacts": {
    "drift": {
      "severity": 0,
      "confidence": 0.0
    },
    "ghosting": {
      "severity": 0,
      "confidence": 0.0
    },
    "wall_duplication": {
      "severity": 0,
      "confidence": 0.0
    },
    "holes": {
      "severity": 0,
      "confidence": 0.0
    },
    "excess_noise": {
      "severity": 0,
      "confidence": 0.0
    }
  },
  "overall_usability_0_3": 0,
  "notes": "<short free-text notes, max ~50 words>"
}
