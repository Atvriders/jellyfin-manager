# Medusa Super-Resolution Pass

**Date:** 2026-07-19 · **Status:** Approved (owner: "more real, super resolution, pop, lifelike, impressive")

Same creature, same behavior, same contract — dramatically better rendering. Iterate on the
shipped Medusa (`5a2d4e9`) inside `createJellyfield3D` only.

## Targets
- **Detail:** bell mesh to ~96×48; finer 8-lobe margin with lappet flutter (high-freq,
  low-amp rim wave); tentacles ~32 chains × ~24 nodes; richer oral-arm frill.
- **Optics:** two-pass bell (back faces then front — see the far wall of the bell through
  the near wall); wrap-light SSS keyed to the shafts; Blinn-Phong wet-glass specular;
  procedural mesoglea density + micro-wrinkles near the margin (fragment noise);
  3-sample spectral rim (real chromatic grazing split); gonad clover drawn with interior
  parallax (moves subtly against the shell = depth inside the body).
- **Pop:** soft-knee tonemap in-shader (bright core, never clips to white soup); layered
  aura billboards behind the bell; local background darkening behind the hero for
  contrast staging.

## Constraints (unchanged from the Medusa spec)
Public contract, selector, 2D engine, templates byte-untouched; edits only inside
createJellyfield3D. GLSL ES 1.00. Draw calls ≤10 (raised from 7 for the two-pass bell +
aura). No per-frame allocation beyond existing dynamic uploads. Context-restore must
rebuild every new resource. Dim band, mobile panel-probe anchor, hidden-pause, debounced
resize, DPR≤2, mount-false-on-failure all preserved. 110 tests green; SwiftShader +
--disable-webgl verification; zero console errors.

## Ascension addendum (owner: "god like look. celestial like.")

Divinity lives in the LIGHT, never the flesh — the creature stays a violet moon jelly;
the water treats her as sacred:

- **Crepuscular convergence:** the light shafts re-aimed to converge on the Medusa from
  the surface — she stands in a column of light, annunciation-style.
- **Aureole:** a diffuse glory/corona behind the bell — soft radiant ring with faint slow
  animated rays, never a hard halo.
- **Ascending star-motes:** tiny twinkling particles RISING around her (celestial dust,
  the inverse of marine snow), dense near her, sparse far away.
- **Apex star-core:** the bell's nucleus elevated to a star-like point with a subtle
  4-point sparkle.
- **Grade:** a restrained white-gold (#F2E4C0-family) permitted ONLY in the shafts,
  aureole, motes, and apex star — the creature's body remains violet/cyan.
- Budget: draw calls may rise to ≤12; all other constraints unchanged (GLSL ES 1.00,
  preallocation, context-restore covers new resources, dim band, mobile anchor,
  reduced-motion → 2D untouched).

## Seraph addendum (owner: "angel from the Diablo games… epic and god like")

She becomes an **angelic being** — the Angiris Council read: radiance, gold regalia,
and wings that are RIBBONS OF LIGHT, never feathers (Tyrael/Auriel). The moon-jelly
body survives underneath; the angel is what she *wears* and *emits*.

### Mandatory fixes (not optional aesthetics — these ship regardless)
1. **Arm attachment.** Tentacles/arms currently read as detached filaments floating
   near the bell. They must visibly GROW from the margin: thick roots tapering to fine
   tips (root width ≥3× current), a marginal collar/mantle where they emerge, root
   luminance matched to the bell rim so the tissue reads continuous, and roots tucked
   slightly UNDER the margin so they emerge from beneath rather than butting against it.
2. **Apex star clipping.** The star core blows to pure white on real GPUs. Tonemap it
   so the core stays luminous AND colored (gold, not white-out).
3. **Anatomy restored.** The aureole is washing out the gonad clover / canals won in the
   superres pass. Pull ambient glow back ~15% and lift interior-organ contrast so the
   photoreal anatomy reads through the divinity.

### Angelic layer
- **Ribbon-wings:** 6–10 long gold light-ribbons sweeping from the upper bell outward
  and upward in a wing silhouette — flowing, tapering, self-luminous, with brighter
  edges. Implemented as a new verlet chain kind (kind 2) so they ride the EXISTING
  ribbon draw call — no new draw call. They must read as wings at a glance, and stream
  with motion, not hang limp.
- **Gold regalia:** filigree accents at the margin/crown — restrained; the bell's flesh
  stays violet/cyan so she remains a jellyfish wearing divinity, not a gold blob.
- **Crown/halo:** the apex star resolved into a defined radiant crown.
- **Vertical presence:** the light column emphasized — High Heavens verticality.
- **Spirit wisps:** fine ascending light-trails shed from the wing tips.
- Interaction: `pulse()` (scan sonar) makes the wings FLARE and sweep — the angel
  reacting; `setActivity` drives their intensity.

### Budget/constraints
Draw calls ≤14. All prior constraints unchanged: edits only inside createJellyfield3D,
GLSL ES 1.00, preallocation (wing chains extend the existing preallocated node arrays),
context-restore covers new resources, dim band, mobile anchor, hidden-pause, DPR≤2,
reduced-motion → 2D untouched, 110 tests green, SwiftShader + --disable-webgl verified.
