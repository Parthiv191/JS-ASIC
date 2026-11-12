"""Constrained-random programs from the compiler (verification.md layer 4) and the rule
cross-check on them: no program the compiler accepts may misbehave on the model.

`randdesc` draws a random two-party description as a recipe (a list of frames), `check` compiles
it, runs it in every role within the description's clock tolerance, applies the oracles, and
shrinks a failure to its smallest recipe. `python -m emu.gen run` is the campaign."""
