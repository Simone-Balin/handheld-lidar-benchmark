# Fault-injection audit -- LLM pipeline reviewer

Protocol: fixed prompt (version 1.0), independent fresh-context repetitions. Majority rule: strict majority of available verdicts; ties count as "no fault".

- Collected: 2026-07-27T11:49:16
- Evaluation set: 10 runs (5 faulted / 5 clean), 2 repetitions planned
- Verdicts: 100 collected, 0 missing, 0 invalid
- Models seen: claude-sonnet-5
- Prompt versions seen: 1.0

## Detection (fault yes/no)

| Level | TP | FP | FN | TN | Recall | Precision | Accuracy |
|---|---|---|---|---|---|---|---|
| Per repetition (pooled) | 47 | 29 | 3 | 21 | 0.940 | 0.618 | 0.680 |
| Per run (majority vote) | 5 | 3 | 0 | 2 | 1.000 | 0.625 | - |

False-positive rate on clean runs (per repetition): 0.580

## Fault-type accuracy

Among true-positive verdicts (fault correctly detected on a faulted run): 39/47 correct type = 0.830

| Ground truth | Reported | Verdicts |
|---|---|---|
| corrupted_log | corrupted_log | 13 |
| corrupted_log | synthetic_outlier | 7 |
| registration_degraded | registration_degraded | 20 |
| synthetic_outlier | registration_degraded | 1 |
| synthetic_outlier | synthetic_outlier | 6 |

## Inter-repetition consistency

Agreement with the modal verdict per run: mean 0.810, min 0.500; unanimous runs: 3/10.

## By fault type

| Fault type | Runs | Verdicts | Detected | Detection rate | Type accuracy | Mean consistency |
|---|---|---|---|---|---|---|
| registration_degraded | 2 | 20 | 20 | 1.000 | 1.000 | 1.000 |
| corrupted_log | 2 | 20 | 20 | 1.000 | 0.650 | 0.750 |
| synthetic_outlier | 1 | 10 | 7 | 0.700 | 0.857 | 0.600 |
| none (clean) | 5 | 50 | 29 | 0.580 | - | 0.800 |

For faulted rows the detection rate is the per-repetition recall; for the clean row it is the false-positive rate.
