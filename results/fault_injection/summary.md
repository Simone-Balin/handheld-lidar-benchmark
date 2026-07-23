# Fault-injection audit -- LLM pipeline reviewer

Protocol: fixed prompt (version 1.0), independent fresh-context repetitions. Majority rule: strict majority of available verdicts; ties count as "no fault".

- Collected: 2026-07-09T14:11:57
- Evaluation set: 10 runs (5 faulted / 5 clean), 2 repetitions planned
- Verdicts: 20 collected, 0 missing, 0 invalid
- Models seen: claude-sonnet-5
- Prompt versions seen: 1.0

## Detection (fault yes/no)

| Level | TP | FP | FN | TN | Recall | Precision | Accuracy |
|---|---|---|---|---|---|---|---|
| Per repetition (pooled) | 9 | 8 | 1 | 2 | 0.900 | 0.529 | 0.550 |
| Per run (majority vote) | 4 | 3 | 1 | 2 | 0.800 | 0.571 | - |

False-positive rate on clean runs (per repetition): 0.800

## Fault-type accuracy

Among true-positive verdicts (fault correctly detected on a faulted run): 8/9 correct type = 0.889

| Ground truth | Reported | Verdicts |
|---|---|---|
| corrupted_log | corrupted_log | 3 |
| corrupted_log | synthetic_outlier | 1 |
| registration_degraded | registration_degraded | 4 |
| synthetic_outlier | synthetic_outlier | 1 |

## Inter-repetition consistency

Agreement with the modal verdict per run: mean 0.800, min 0.500; unanimous runs: 6/10.

## By fault type

| Fault type | Runs | Verdicts | Detected | Detection rate | Type accuracy | Mean consistency |
|---|---|---|---|---|---|---|
| registration_degraded | 2 | 4 | 4 | 1.000 | 1.000 | 1.000 |
| corrupted_log | 2 | 4 | 4 | 1.000 | 0.750 | 0.750 |
| synthetic_outlier | 1 | 2 | 1 | 0.500 | 1.000 | 0.500 |
| none (clean) | 5 | 10 | 8 | 0.800 | - | 0.800 |

For faulted rows the detection rate is the per-repetition recall; for the clean row it is the false-positive rate.
