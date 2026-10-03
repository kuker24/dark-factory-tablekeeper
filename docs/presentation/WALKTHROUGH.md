# Walkthrough video — plan and narration script (~3.5 min)

`final-demo.mp4` is the silent, captioned time-lapse cut from the raw recording of BAND Desktop on the
box. If you want a narrated version, record a voice-over to the script below in any editor; the timings
match the cut.

| Time | On screen | Narration (suggested) |
|---|---|---|
| 0:00–0:06 | Intro card | "This is Tablekeeper, built by a dark factory: three AI seats in one Band room, and a human who posts one task per stage and nothing else." |
| 0:06–0:10 | Stage 1 card | "Stage one: the reservations API. One task message, pasted spec, and the official check command." |
| 0:10–0:52 | Stage 1 time-lapse | "The Architect writes a requirements ledger and sends work orders. The Coder builds; the Reviewer writes its own probes from the spec in parallel. Watch the rejections: the Reviewer finds defects the shipped tests never exercise, and the Coder fixes them, all without us." |
| 0:52–1:38 | Stage 2 card + time-lapse | "Stage two adds a booking UI. The Reviewer drives it in a headless browser and rejects what doesn't meet the spec: two rejections, then the Coder spends over an hour hunting a timing race in one browser test before it is accepted." |
| 1:38–2:24 | Stage 3 card + time-lapse | "Stage three: policies, history, recurring series. The Coder builds it in twenty minutes and hands it off. Then the Reviewer's turn is cut off by context compaction, nothing wakes it again, and the room goes quiet. Our timebox ends the run thirty minutes later, without a word into the room. Stage three is built but unreviewed, so we don't claim it." |
| 2:24–2:32 | Result card | "Two stages accepted, verified by our own isolated check on a fresh clone; four and a half hours, about twenty-seven dollars. The repository has the mandates, the reply gate, the full room log, and an honest list of what broke, including one Coder message that went out under my account, and how we fixed it afterwards." |

Suggested extra shots (optional, record by hand if you want them):
1. `FACTORY.md` diagram in the GitHub view (5 s).
2. `docs/test-results/SUMMARY.md` (5 s).
3. The room in the Band web console, scrolled to one REJECT verdict (5 s).
