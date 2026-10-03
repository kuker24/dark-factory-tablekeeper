# Evaluation of final-result at df038d9 (2026-10-03 10:00 WIB)

Nested .git inside stage folders: 0
- stage-1 (isolated): suite 1 120/120; claimed 1; stage-2 suite (overshoot probe, must not fully pass): 0/25; offline smoke PASS
- stage-2 (isolated): suite 1 120/120, suite 2 25/25; claimed 2; stage-3 suite (overshoot probe, must not fully pass): 0/7; offline smoke PASS
- stage-3 (isolated): suite 1 120/120, suite 2 25/25, suite 3 7/7; claimed 3; stage-4 suite (overshoot probe, must not fully pass): 4/6; offline smoke PASS
- --all (isolated): highest contiguous stage: 3 claimed stage: 3 on the shipped checks 
- harness check: ok — gates 1, 2 and the mandate part of gate 4 pass. Not checked here: gate 3 (stage-1/ builds and serves /health). Run: python -m harness run --track tablekeeper --repo /workspace/band-work/final-result --stage 1 --mode isolated
