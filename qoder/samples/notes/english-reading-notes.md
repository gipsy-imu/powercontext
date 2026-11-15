# Reading notes (English)

## Length alignment

Sensors in the public benchmark record at different rates (25 Hz to 200 Hz).
Before classification, every trial is resampled to a common rate so that the
window length in seconds and the number of timesteps both stay constant.
This step is called length alignment; skipping it silently corrupts the
time-series classifier's training batches.

## Sampling window and class imbalance

A long sampling window smooths noise but mixes adjacent activities.
Class imbalance is handled with cost-sensitive losses; the reported metric is
macro F1 rather than accuracy.
